# Broker service and remote agents

Status: draft (proposed). This document is the plan; the code lands phase by
phase and each phase updates the checklist at the end.

## Motivation

Today every cabildo participant opens the shared SQLite database directly:
`store.connect()` is called by each session's MCP subprocess, by every hook
invocation, by the CLI, the TUI, and the daemon. This is deliberate and it is
why the single-machine case needs no server: if two processes share a
filesystem, they share a bus.

Direct database access has one hard limit: a process on another machine cannot
open the file. To let a cabildo agent run on a remote host (a cloud box you
provision with terraform, libcloud, a GPU box, or any other means) and join your
bus, the agent needs a network path to the bus, not a filehandle on it.

The fix is to introduce a broker. One long-lived service owns the database; every
other participant becomes a client that talks to the service over IPC. Local
clients use a unix socket; a remote client reaches its own local service, and the
services sync to each other over the network. Local and remote agents become
symmetric: both are just clients of a service.

## Current architecture

```
  ┌── session A ──┐   ┌── session B ──┐
  │ mcp subprocess│   │ mcp subprocess│      hooks (short-lived processes)
  │  store.connect│   │  store.connect│      cli / tui / daemon
  └───────┬───────┘   └───────┬───────┘             │
          └──────────┬────────┴───────────────┬─────┘
                     ▼                         ▼
              ┌──────────────────────────────────┐
              │        one SQLite file (WAL)       │
              └──────────────────────────────────┘
```

- `store.py` (1725 lines) is the data layer: schema, migrations, and every
  query/mutation. `store.connect()` opens the file with `journal_mode=WAL`,
  `busy_timeout=5000`, `foreign_keys=ON`.
- ~60 `store.connect()` call sites across `mcp_server.py` (20+),
  `hooks/*` and `codex_hooks.py` (short-lived), `cli.py`, `actions.py`, `tui.py`,
  `lan.py`, `daemon.py`.
- `daemon.py` is a single-threaded worker that drains the intents queue
  (land / spawn / ask), one action at a time, plus periodic sweeps (reap dead
  sessions, ingest opted-in transcripts, sample machine conditions). Started with
  `cabildo service`. It is the natural home for the broker.
- `lan.py` already syncs messages between machines on one subnet: UDP-broadcast
  discovery, TCP pull, `guid` dedup, `origin='lan'` tagging, optional shared
  `secret` on the pull handshake, no transport encryption. Stdlib only. It is the
  basis for the WAN transport.

## Goals and non-goals

Goals:

- A long-lived service that owns the database and serves the store surface over
  local IPC.
- MCP processes and hooks work as clients of that service.
- A remote agent on another machine joins the bus over an authenticated network
  transport and can run `request(action="spawn")` to launch a local agent
  (claude / codex / opencode) on the remote host.

Non-goals:

- Provisioning or starting machines. cabildo assumes the remote box already
  exists and can run `cabildo`; how it was started (terraform, libcloud, a
  console, a script) is out of scope.
- Replacing the zero-server default. The broker is **opt-in**. When no service is
  running, clients fall back to opening the database directly, exactly as today.
  The README promise ("no daemon, no network" for the single-machine case) holds.

## Design

### The Bus seam

Introduce one interface that every current `store.connect()` call site goes
through, so there is a single place that decides "direct SQLite" vs "talk to the
broker":

```
class Bus(Protocol):
    def call(self, op: str, **params) -> Any: ...
```

- `LocalBus`: opens `store.connect()` and runs the op against `store`. Behaviour
  identical to today.
- `SocketBus`: encodes the call as a request to the service over a unix socket
  and returns the decoded response.

Selection: use `SocketBus` when a service socket is present and reachable,
otherwise `LocalBus`. This keeps the single-machine, no-service path working and
makes the broker an optimisation/enabler rather than a hard dependency.

The op surface is **per operation, not per `store` function.** Two findings from
the code map force this:

- **Transactions span multiple `store` calls.** `join_topic` is
  `ensure_topic` + `subscribe` + one `conn.commit()`; `ensure_topic`,
  `subscribe`, and `unsubscribe` do not self-commit on their own. A naive "one
  RPC per store function" would split that transaction across round trips. So an
  op like `join_topic` is one broker call that runs the whole transaction on the
  owning side.
- **Identity is caller-local.** `mcp_server._me` resolves "who am I" from the
  calling process's environment (`CABILDO_TAG`, `CABILDO_SESSION_ID`,
  `CABILDO_CLIENT`) and PID ancestry (`os.getppid`, `context.resolve_claude_pid`).
  A broker in the daemon cannot see the client process's env or parent PID, so
  every RPC carries an explicit identity envelope (tag / session-id / owner-pid /
  client) and the broker resolves the session from that, not from its own
  process. Resolution logic stays shared; only its inputs move onto the wire.

### Local IPC

- Transport: a unix domain socket at `store.home()/bus.sock` (a bound TCP port on
  loopback is the fallback where unix sockets are unavailable).
- Framing: length-prefixed or newline-delimited JSON. One request → one
  response. Stdlib only (`socket`, `json`), matching the rest of the package.
- Surface: the broker exposes the subset of `store` functions that clients call
  today (the writes and reads enumerated during phase 1). No generic "run this
  SQL" op ever, for the same reason `actions.py` has no generic "run this
  command": a named op the caller cannot forge is the boundary.
- Auth (local): filesystem permissions. The socket is created mode 0600, owned by
  the user; only that user's processes can connect. No token and no TLS on the
  local socket: it is kernel-local, so transport encryption adds cost and no
  security. The loopback-TCP fallback is the exception, since a loopback port has
  no file-permission gate: there the client must present the bus token.
- Concurrency: the broker serialises writes through the existing single-threaded
  daemon, which removes SQLite multi-writer contention (the `busy_timeout` waits)
  entirely.

### WAN transport

Services, not agents, sync across machines. Each host runs one service; a remote
service is a peer.

- Reach: cabildo is **transport-agnostic**. It connects to a peer at `host:port`
  and pulls; it does not care how the bytes are carried, so the tunnel is a
  deployment choice, not cabildo code. Two supported shapes:
    - **WireGuard / Tailscale mesh**: the peer address is the mesh IP (for example
      `100.x.y.z:48653`). WireGuard encrypts and mutually authenticates the
      channel; Tailscale adds OIDC device identity and ACLs.
    - **Plain SSH tunnel**: `ssh -L 48653:localhost:48653 <box>` forwards a local
      port and cabildo points at `localhost:48653`; SSH provides the encryption
      and host/user authentication. A remote forward (`-R`) works the same way.
  cabildo does not implement its own tunnel or NAT traversal, and it **binds only
  to the tunnel or loopback interface, never `0.0.0.0` on the public internet.**
- Sync: reuse `lan.py`'s pull model and `guid` dedup, but with **explicit peer
  addresses** (from config) instead of UDP broadcast discovery, since broadcast
  does not cross subnets. Pulled rows are stamped `origin='wan'` (kept out of the
  cloud uplink, like `origin='lan'`).
- Auth (v1): a shared bus token proves membership on the pull handshake (the
  `secret` hook `lan.py` already has). It rides on top of the already-encrypted
  tunnel, so the token is authorization (which peers may join this bus), not the
  encryption layer.
- Auth (later): per-agent signed rows. Each peer holds an ed25519 keypair, signs
  every row it syncs, and the receiver verifies and records a verified identity.
  This gives per-agent accountability even over an untrusted relay and can be
  added without changing the transport.

### Remote spawn

- `request(action="spawn")` gains an optional `peer` (or `host`) parameter naming
  the target service.
- The intent syncs to the target service, whose daemon (already the single
  serialisation point) runs the spawn locally and reports the outcome back over
  the synced bus, exactly as a local spawn reports today.
- The client on the remote host (claude / codex / opencode) is chosen from what
  is installed there; the existing client-config abstraction already knows how to
  launch each.

## Security and trust

Opening the bus to a remote machine that can `spawn` is a remote-code-execution
channel by design. The network edge becomes a real trust boundary, which the
single-machine model never needed. Rules:

- Remote spawn is off by default and enabled per peer, the same shape as the
  daemon's existing `--allow-push` / `--allow-spawn` gates (for example
  `--allow-remote-spawn <peer>`).
- The local unix socket is user-only (mode 0600); the WAN transport requires the
  shared token and rides the encrypted tunnel.
- `SECURITY.md`'s trust model is updated: with a WAN peer connected, "anyone who
  can open the database is trusted" no longer holds, and the token plus the tunnel
  identity are what gate a peer.

### Encryption, by layer

Encryption and channel authentication belong to the transport; cabildo does not
reimplement them.

- **Local unix socket**: no encryption. Filesystem permissions (0600) are the
  boundary; a kernel-local socket has no network to protect.
- **WAN**: the tunnel encrypts and authenticates the channel (WireGuard's static
  keys, Tailscale's device identity, or SSH's host/user keys). cabildo adds the
  shared token for authorization on top of that encrypted channel.
- **Optional app-level TLS**: cabildo can wrap its socket with the stdlib `ssl`
  module for a deployment that exposes the port without a tunnel. This is opt-in,
  requires operator-supplied certificates, and is redundant when a tunnel is
  already in place, so it is not the v1 default.

## Phased plan

Each phase keeps `make check` green and lands as its own commit. No push. The
foundation (phases 0-3a) is additive: nothing in the live hooks/MCP has changed.
Two later steps are sensitive and gated on an explicit go-ahead: **3b** touches
the tooling running the current session, and **5** is the remote-code-execution
surface. Current order: 4 next, then 3b and 5 once reviewed.

- [x] **Phase 0 — this design doc.** Committed; nav updated.
- [x] **Phase 1 — the Bus seam.** `bus.py`: `Bus` protocol, the op surface (per
  operation, explicit identity envelope), `LocalBus`, reads + writes +
  transactional ops, tested. Additive; no call site routes through it yet.
- [x] **Phase 2 — the broker server.** `broker.serve` (unix socket, one owned
  connection under a lock) + `SocketBus`; the daemon serves it on its own thread
  unless `--no-broker`/`--once`. Tested, including a daemon integration test.
- [x] **Phase 3a — client selection.** `bus.default_bus()` picks `SocketBus` when
  a broker is live (probed), else `LocalBus`; `CABILDO_BROKER=0` forces local.
- [ ] **Phase 4 — WAN sync (next).** Service-to-service pull over explicit peer
  addresses with the shared token; `origin='wan'`; reuse `lan.py`'s guid dedup.
  Additive, no live-tooling change.
- [ ] **Phase 3b — migrate the hot path (gated).** Route the MCP tools + hooks
  through `default_bus()`. Touches live tooling and must reproduce each tool's
  full logic server-side, so it waits for a go-ahead.
- [ ] **Phase 5 — remote spawn (gated).** `peer` param routing and per-peer
  gating. This is the remote-code-execution surface, so it waits for a go-ahead.
- [ ] **Phase 6 — polish.** Config wiring (`[remote]`, token), `cabildo doctor`
  checks, `SECURITY.md` update, an end-to-end test of the remote-agent path.

## Open questions

- Socket framing: **resolved** — newline-delimited JSON with a size cap
  (`bus.MAX_LINE`), for debuggability.
- Whether phase 3b should make the service auto-start on first client use, or stay
  explicit (`cabildo service`). Auto-start is convenient but reintroduces a
  daemon the user did not ask for; explicit is the safer default.
- Whether the ed25519 signing (WAN auth "later") should land before opening
  remote spawn to peers outside a fully trusted mesh.

## Why stdlib, not a message-queue library

A message-queue library (ZeroMQ via pyzmq is the obvious one) would hand us
ROUTER/DEALER for the broker, PUB/SUB for message fan-out instead of polling,
`ipc://` and `tcp://` transports, and CurveZMQ for the encryption and peer
authentication the WAN case wants. It is a genuinely good fit.

It is still not adopted, for one reason: cabildo is stdlib-only at runtime (the
sole runtime dependency is `mcp`), and pyzmq bundles a native library. That rule
is worth keeping for a tool that installs into every agent's environment. The
`Bus` seam is what makes the choice reversible: the wire transport lives entirely
in `broker.py` and `SocketBus`, so swapping the hand-rolled unix-socket +
newline-JSON layer for ZMQ later would touch those two places, not the call
sites. Decision: stdlib for now, seam keeps the door open.

## Prior art

Other MCP coordination buses run a single broker process that agents connect to,
rather than sharing a database file: agent-bus-mcp, agent-comm, claude-peers-mcp.
This design adopts the broker model for the cross-machine case while keeping the
shared-file model as the zero-config local default.
