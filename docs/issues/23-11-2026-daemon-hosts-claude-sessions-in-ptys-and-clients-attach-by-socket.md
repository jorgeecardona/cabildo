---
title: Claude sessions have no owner process, so only the TUI can show them live
status: in-progress
scope: L
owner: cute-baymax-2219f
filed: 2026-11-23
area: daemon
verify: probe
blocked_by: null
fixed_in: null
---

# Claude sessions have no owner process, so only the TUI can show them live

Goal: the cabildo daemon (or a small process next to it) owns each Claude session in a
pseudo-terminal. The console, the TUI, and a VS Code extension attach to it as viewers.

## Problem

Only the TUI workspace can show a live Claude session, because only the TUI owns its
pseudo-terminal. A session started any other way has no viewer to attach to. A VS Code
extension cannot show a live session today.

## Measured

- `src/cabildo/workspace/hosting.py`: the TUI runs one Claude per pane, each in its own
  pseudo-terminal. Closing a pane closes the pseudo-terminal, and Claude exits (SIGHUP).
- `src/cabildo/workspace/attach.py`: a TUI reload keeps the sessions alive. It passes the
  master file descriptors through `os.execv`. It depends on private API of `bittty`, which is
  pinned for that reason.
- `src/cabildo/sessions_ops.py:257`: `create` starts `claude -p --session-id` with
  `start_new_session=True` and no pseudo-terminal. Nothing owns that process afterwards.
- `src/cabildo/daemon.py`: the daemon serves the broker on a unix socket (`bus.sock`). The
  gateway keeps its listening socket across a daemon reload.
- `src/cabildo/service.py:236`: the systemd unit is `Type=simple` with `Restart=always` and no
  `KillMode` line.

## Inferred (not checked)

- With the default `KillMode=control-group`, `systemctl --user restart` kills every child of the
  daemon, so children of the daemon die on each restart. Check with a test unit before building.
- A daemon crash closes the master file descriptors, so every hosted Claude gets SIGHUP.
- A new viewer sees a blank screen until Claude repaints, unless the owner keeps a screen
  buffer.

## Design

Data first. One frozen dataclass per hosted session: `session_id`, `cwd`, `command`, `pid`,
`rows`, `cols`, `started_at`. The owner keeps a table of these. Viewers get the same record.

1. The owner starts `claude` in a pseudo-terminal and holds the master file descriptor.
2. The owner serves each session on a unix socket. The wire carries raw bytes in both
   directions, plus a small control message for resize.
3. The owner keeps a screen buffer per session (same engine as the TUI), and sends the current
   screen to a viewer when it attaches.
4. Many viewers per session. All see the output, all can type. The window size follows the
   most recent viewer.
5. `cabildo attach <session>` connects the console to the socket. The TUI uses the same
   connection instead of its own pseudo-terminal.
6. A VS Code extension (separate issue) uses the same socket as the backend of a terminal tab.
7. Restart: pass the master file descriptors through `exec` on reload, as the gateway does with
   its socket. For a crash, run the owner as a separate small process that only holds
   pseudo-terminals, so the daemon can restart or crash without killing sessions.

`sessions_ops.create` moves to the owner, so a created session has a parent and a viewer.

### Sessions in different folders

Each session has its own `cwd`, and the owner starts `claude` in that folder. Claude then reads
`CLAUDE.md`, `.mcp.json` and `.claude/` from that folder, as it does today.

The owner runs as a systemd user service, so a spawned session gets the service environment, not
your shell's. Variables such as `SSH_AUTH_SOCK` or a project `.env` are missing unless the owner
sets them at spawn. Decide where they come from: the client that asks for the session sends
its environment, or the owner loads a fixed list. Add the chosen variables to the session record.

### The TUI becomes a client

The TUI stops owning pseudo-terminals. Remove most of `workspace/hosting.py` and all of
`workspace/attach.py` (the reload handoff through `exec`). A TUI reload then reconnects to the
sockets.

The TUI still needs a terminal widget (`textual-tty`) to draw each session's bytes. The VS Code
extension does not, because its terminal tab draws them. The TUI panels (spend, heatmap, rooms,
notes) do not change.

## Done when

1. `cabildo attach <id>` shows the live screen of a session the daemon started.
2. Two viewers on one session both see output, and typing from either reaches Claude.
3. `systemctl --user restart cabildo-daemon.service` leaves hosted sessions running, and a
   viewer reattaches to the same screen. Verified on a real daemon (`probe`).
4. A test covers attach, detach, and resize on a fake child process.
5. Two sessions with different `cwd` each read the `CLAUDE.md` of their own folder.
6. The TUI shows a session the daemon started, and a TUI reload does not interrupt it. `workspace/attach.py` is deleted.

## First slice (done, 24-11-2026)

Decided: the owner is a separate process, `cabildo host` (`src/cabildo/host.py`), not a thread of
the daemon. A daemon restart would close every master descriptor. Clients start it on demand.

- Done: `host.py` (socket server, frames, start, adopt by `SCM_RIGHTS`, tag, kill),
  `host_client.py`, `cabildo host`, `cabildo attach`, and the workspace option
  `[sessions] owner = "host"` (new, resume, quit leaves sessions running, reload adopts the
  Claudes the app owns). Tests: `tests/test_host.py`, `tests/workspace/test_hosted.py`.
- Environment: the client sends its environment with `start`, so `SSH_AUTH_SOCK` and the rest
  reach Claude.
- Not done: a screen buffer (a new viewer sees a repaint, not the scrollback), a systemd unit
  for the host, the `KillMode` and crash checks, a detach key for `cabildo attach`, the
  start-folder limit, `sessions_ops.create` in the host, deleting `workspace/attach.py`.
- Probe still missing: the option has not been run against a real `claude` session.

## Not doing here

- The VS Code extension. It is a client of this socket and needs its own issue.
- Remote or cross-machine attach. LAN and WAN sync carry messages, not terminals.
- Replacing `claude agents` for session discovery.
- Surviving a reboot. A reboot ends the processes.

## Open questions

- Owner inside the daemon, or a separate process? The separate process survives a daemon crash
  but adds a unit to install.
- Which engine keeps the screen buffer? `bittty` is private API today. Check for a stable
  alternative before adding a second use of it.
