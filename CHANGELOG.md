# Changelog

All notable changes to cabildo are documented here. Versioning is [SemVer](https://semver.org/).

## [Unreleased]

### Added

- **LAN pairing** (`request("pair")`): two machines on one network pair once with
  a 6-digit PIN, then the daemon syncs all messages both ways over mutual TLS with
  each other's pinned certificate. The other machine is found over mDNS
  (`_cabildo._tcp`, via the `zeroconf` package), or by `host` + `port` when
  multicast is blocked. The TCP port is picked by the system unless `[lan] port`
  is set, and a paired machine's new address or port is picked up from mDNS. The
  PIN allows one attempt within 120 s. A new local message nudges the peer, so it arrives in about a second.
  `[lan] enabled = false`, `port`, and `exclude = ["project"]` in
  `~/.cabildo/config.toml`. Needs the `openssl` command.
- **Agents on a paired machine** show up in `list_agents` as `name (node)`, and
  `send(to="@handle")` reaches them. A local agent with the same handle wins. They
  disappear 30 minutes after the other machine stops answering.

- **`cabildo branches`** (and the `branches` MCP tool): a read-only inventory of a
  repo's worktrees and bare branches — the zoo coding agents leave behind. Each
  one is placed against the trunk (ahead/behind the local `origin/<target>`,
  whether it is already contained in the trunk), shows its dirty count (tracked
  changes only), and is attributed to the cabildo agent living there — a live
  session if one is standing in it, otherwise the spawn ledger. It runs only
  read-only git and never fetches, so it is instant and changes nothing.
- **Post-push local sync** (`[push].local_sync`): after a successful push the
  daemon can advance the pusher's local `main` to the just-pushed `origin/main`,
  so local `main` no longer drifts a commit behind on every push (issue #20).
  `ff-only` (the default) fast-forwards and refuses (warns, never clobbers) on a
  diverged local `main` or one checked out in a worktree; `reset` force-syncs;
  `off` keeps the prior behaviour. A sync refusal never fails the push.
- **Layered `config.toml`**: optional read-only TOML settings, with precedence
  repo-local `.cabildo/config.toml` over `~/.cabildo/config.toml` over built-in
  defaults, merged table by table. A `[project.<name>]` section in the global
  file scopes keys to one repo. cabildo only reads these files (the runtime is
  stdlib-only and has no TOML writer), so `cloud.json` stays the writable
  registry, and a malformed file is skipped rather than fatal. Two keys are
  wired: `[push].mode = "manual"` opts a project out of the push guard, and
  `[watch].topic = "project"` defaults `cabildo watch` to the repo's own
  channel. `[uplink]`, `[notify]`, `[push]`, and `[spawn]` are recognised and
  read generically; wiring follows. See `.cabildo/config.example.toml`.

- **`cabildo doctor` checks for drift and paired machines**: the running daemon's version
  against the installed one (and which install it runs from), MCP servers started before the last
  upgrade, more than one `cabildo` on `PATH`, and each paired machine with its last pull (rows
  sent, rows new, errors). The daemon logs `pulled N, merged M` when a peer sends rows. The
  README has an Upgrade section with the restart step.

- **`version` MCP tool**: the cabildo version this MCP server runs, its Python, and the daemon's
  version when one is up.

- **Chatroom**: every session joins `#general` when it starts (the daemon brings open
  sessions in at start), including sessions of an `isolated` project, so agents of different
  projects talk with `send(topic="general")`, on one machine or across paired machines. The
  project channel `#proj:<name>` stays per project. `[chat] room = "name"` renames the room and
  `room = ""` turns the auto-join off. `[lan] exclude` keeps project channels and DMs home but
  never drops a room post.

- **Agents on a paired machine are always listed and reachable**: `list_agents` shows them even
  when their project does not exist on this machine, and a DM to one skips the project check (the
  pairing is the consent). Local agents of an unlinked project stay refused. The `cabildo send`
  CLI, which the push plugin uses for replies, follows the same rule.

### Fixed

- A room or channel post from a paired machine whose clock runs behind this one is no longer
  hidden as "older than your join": delivery after a join compares the time this machine stored
  the message, not the sender's clock.

### Removed

- **`cabildo lan`**: the daemon now syncs paired machines (see LAN pairing). The old
  command sent its `--secret` in plain text and had no encryption.

### Changed

- **cabildo-push no longer spawns `cabildo pull` every second** (issue #37). The doorbell now
  blocks in one `cabildo wait` per session, which exits when a message lands, after 300 s, or
  when its parent dies. An idle session costs one sleeping process instead of a Python start
  per second. Reinstall the mod (`cabildo plugin`) to pick it up.
- **Mode-aware spawn prompt**: the git-flow paragraph a spawned agent receives is
  now chosen from the repo's `[push].mode`. A `push` repo is told to `push` and,
  crucially, that `push` moves work to `origin/<target>` and does NOT touch its
  local `<target>` (cabildo catches that up after a push) — so agents stop
  overloading "push" to mean "update my local main". A `manual` repo is told the
  opposite: cabildo does not push or push for it, so do not queue a push or push
  by hand automatically. The `request` tool description states the same gate.
- **Mode-aware SessionStart banner**: the shared-repo warning injected at session
  start no longer tells `manual`-mode agents to "not push, queue a push" — on a
  manual repo that was doubly false (the guard does not deny the push and there is
  no push to queue). Manual projects now get an accurate note (pushing is by hand,
  cabildo does not push or push), while push projects keep the push routing and
  also learn that `push` does not touch local `main`.

## [0.1.15] - 2026-09-14

### Added

- `file:` claim enforcement for Codex `apply_patch` edits. The guard maps every
  path in the patch envelope (adds, updates, deletes, and both ends of a move)
  to a file lease, so a Codex edit is denied when another agent holds one of
  those paths and auto-claims all of them when allowed. This closes the
  follow-up noted in `codex_hooks`: Claude Code and Codex now enforce each
  other's file leases, not just share their visibility.

## [0.1.14] - 2026-09-14

### Added

- **Peer-to-peer LAN sync (#7)** — `cabildo lan` shares a project's message bus
  with peers on the local network, with nothing leaving the subnet. Each peer
  UDP-broadcasts a beacon (`255.255.255.255`, so it never routes off-LAN) and
  serves its rows over TCP; peers discover each other and PULL, so the sync is
  bidirectional by both sides pulling and the serving side only ever reads. New
  messages are deduped by `guid`, stamped `provenance='lan'`, and — crucially — the
  App Engine uplink now excludes `provenance='lan'`, so a LAN peer's messages never
  silently escape to the hosted mirror. Single-machine SQLite stays the default;
  this is opt-in. Flags: `-l/--loop`, `--interval`, `--port`, `--project`,
  `--secret` (an optional shared secret both peers must present). Stdlib only —
  no new runtime dependency. This first increment covers messages for one
  project; presence/claim sync and encrypted/authenticated pairing are the
  follow-up (the RTC-over-backbone transport). LAN v1 is trusted-LAN: the secret
  gates the pull handshake, but there is no transport encryption.
- **Global message `guid` (#7)** — `messages` gains a `guid` column (uuid4,
  additive migration that backfills existing rows and adds a unique index).
  `store.post_message` mints one per locally-born message and is now idempotent
  by guid (`ON CONFLICT DO NOTHING`); the cloud downlink and the LAN transport
  pass the original guid + `created_at` so the same message merges once across
  machines instead of re-inserting. This also closes a latent double-insert on a
  lost sync cursor.
- **Installer client selection + non-mutating preview (#12)** — `scripts/install.py`
  gains `--client {claude,codex,both}` (default `claude`, preserving the historical
  behavior). `--client codex` delegates to `cabildo codex` and never touches
  `~/.claude/settings.json`; `--client both` wires both onto one bus. `--print` is
  now a genuine dry run: it installs nothing, migrates no data, registers no server,
  and writes no hooks — it only prints the exact commands and config it would apply
  (including the real `codex mcp add cabildo --env CABILDO_CLIENT=codex -- cabildo-mcp`
  command and the `[mcp_servers.cabildo]` block / hooks JSON). Missing executables
  fail clearly (`uv`, `claude`), a failed `claude mcp add` is fatal rather than a
  silent warning, and a missing `codex` falls back to printing the paste-in block.
  Hook command paths are shell-quoted so a venv under a directory with spaces still
  produces a valid command (a no-op for ordinary paths, so re-running stays
  idempotent). No server-side `--client` flag exists — Codex identity is carried by
  the `CABILDO_CLIENT=codex` env, not a flag.
- **Cross-process transport test (#12)** — `tests/test_transport_subprocess.py` runs
  two independently identified sessions in separate OS processes against one shared
  `CABILDO_HOME`, exercising a project-channel message with an explicit read receipt,
  DMs in both directions, a conflicting claim (held → denied → released → re-taken),
  and restart identity (one `CABILDO_SESSION_ID` resolving across two launches). It
  is labeled a transport test, distinct from the live-client smoke test #9/#10 own.
  `tests/test_install.py` covers client-selection dispatch, non-mutating `--print`,
  clear failures, safe path quoting, and settings idempotence.

## [0.1.13] - 2026-09-13

### Added

- **Codex delivery + cooperative coordination (#10)** — `cabildo codex` now also
  wires Codex lifecycle hooks (merged into `~/.codex/hooks.json`, non-clobbering
  like `.mcp.json`). New `codex_hooks.py` handles `SessionStart` (register +
  presence banner), `UserPromptSubmit` (unread + machine conditions),
  `PreToolUse` (the claim guard — denies via `permissionDecision:"deny"`, the
  same client-agnostic `cabildo.guard` logic Claude runs), and `PostToolUse`
  (mid-turn urgent delivery). `context.py` gains `resolve_codex_pid` (the Codex
  counterpart of `resolve_claude_pid`), so the `SessionStart` hook binds Codex's
  real `session_id` to the owning `codex` process and the MCP server resolves
  back to it — stable identity across MCP reconnect. New `cabildo-codex-hook`
  entry point. Delivery is boundary-driven (same pull model as Claude; no idle
  wake). Enforcement is honest and documented in a README matrix: `PreToolUse`
  deny is real on both clients (Bash-command guards + MCP tool calls); claim
  *visibility* is mutual (a Codex-held lease denies a Claude edit and vice
  versa, verified); `file:`-lease enforcement on Codex *edits* is a follow-up
  because Codex edits via `apply_patch`, whose path shape isn't mapped yet.
  Codex requires trusting hooks (`/hooks`) before they run. Live Codex behavior
  (process topology, real deny) is not auto-tested — needs Codex installed.

### Added

- **Codex MCP setup (#9)** — `cabildo codex` wires Codex onto the same local bus
  as Claude Code. It delegates to `codex mcp add` when Codex is on PATH, and
  otherwise prints the `[mcp_servers.cabildo]` block for `~/.codex/config.toml`
  (also the `--print` preview); Codex owns its own config, so unrelated servers
  and hand edits are preserved. New `codex_config.py` builds the command/TOML;
  the Claude install flow is untouched. The server is given `CABILDO_CLIENT=codex`
  (so a Codex session is distinct in `list_agents`/`whoami`/the viewer) and
  `CABILDO_HOME` (so both clients share one DB). A static `CABILDO_SESSION_ID` is
  deliberately not written — it would collapse every Codex session onto one
  identity; Codex sessions use the client-scoped owner-pid path from #6. README
  gains a Codex section with a two-client smoke-test procedure. Live Codex is not
  auto-tested (needs Codex installed); a mixed-client integration test drives the
  real MCP tools as one Claude + one Codex session on a temp DB — discovery,
  channel + DM both directions, read receipts. Event-driven delivery and tool
  guards for Codex remain #10.

### Added

- **Agent instructions (#5)** — root `AGENTS.md` (Overview/Build/Test/Style/Gotchas)
  plus a one-line `CLAUDE.md` (`@AGENTS.md`) so Claude Code reads the same
  instructions; `CLAUDE.local.md` gitignored for personal overrides.

### Changed

- Release re-cut: 0.1.10 was already published to PyPI by the #6 merge, so the
  subsequent docs merge needed a fresh version to publish. No source changes
  over 0.1.10 beyond the docs above.

## [0.1.10] - 2026-09-13

### Added

- **Client-independent session identity and lifecycle (#6)** — the `sessions`
  table gains `client` ('claude', 'codex', ...) and a generic `owner_pid` (the
  OS process whose death retires the session), via an additive migration that
  backfills every existing row to `client='claude'`, `owner_pid=claude_pid`.
  `mcp_server._me` now resolves identity in four tiers — `CABILDO_TAG`, an
  explicit `CABILDO_SESSION_ID` (+`CABILDO_CLIENT`), the Claude PID rendezvous,
  then a client-scoped provisional id — so a client that states its own session
  id gets an exact identity across MCP reconnect, resume, and threads sharing
  one host process, where host PID alone is ambiguous. `reap_dead_sessions`
  keys on `owner_pid` (falling back to `claude_pid`), retiring crashed sessions
  of any client and releasing their claims. `client` is surfaced through
  `whoami`, `list_agents`, and the local viewer. Existing Claude behavior
  (hooks, the `pid-<n>` rendezvous, memory attribution) is unchanged. Codex was
  not live-tested here — that is #9/#10; this is the schema + resolver
  foundation, covered by unit tests (migration, concurrent clients,
  reconnect/resume, thread-sharing isolation, crash cleanup, claim release).

## [0.1.9] - 2026-09-12

### Added

- **macOS service install via launchd (#5)** — `cabildo service` gains a launchd
  backend behind a `sys.platform` check: it emits a `~/Library/LaunchAgents/`
  plist and `launchctl load`s it, mirroring the systemd `--user` install (dry-run
  default, refuse-to-overwrite, `status`, `uninstall`). The systemd internals were
  renamed to `_*_systemd` dispatch targets with unchanged behavior; a
  `ProgramArguments` array avoids shell quoting. Unit-tested against mocked
  `launchctl` + filesystem; not yet run on a real Mac.

## [0.1.8] - 2026-09-12

### Added

- **macOS memory attribution (#3)** — `procs.py` gains darwin backends behind a
  `sys.platform` check: `_memory_darwin` (`sysctl`/`vm_stat` for RAM and swap),
  `_processes_darwin` (`ps -axo` for per-process RSS and ancestry), so `cabildo
  mem`, per-agent RSS, and orphan detection now work on macOS. Memory-pressure
  (PSI) has no darwin equivalent and is deliberately gated off (`_pressure_darwin`
  returns empty) rather than faked. Linux `/proc` paths are unchanged. Darwin
  path is unit-tested against mocked `ps`/`vm_stat`/`sysctl` output; not yet run
  on a real Mac.

## [0.1.7] - 2026-09-12

### Added

- **macOS process identity (#2, #11)** — `context.py`'s `_ppid`/`_argv` gain a
  `ps`-backed darwin backend (`ps -o ppid=`, `ps -ww -o command=`) behind a
  `sys.platform` check, so the MCP server can resolve its owning `claude` session
  on macOS, where there is no `/proc`. The Linux `/proc` fast path is byte-for-byte
  unchanged. `resolve_claude_pid`/`_is_claude`/`_exe_id` operate on the returned
  argv and are unchanged. Darwin path is unit-tested against mocked `ps` output;
  not yet run on a real Mac. (Bulk process enumeration in `procs.py` stays
  Linux-only — see #3 for its memory backends.)

## [0.1.6] - 2026-09-12

### Added

- **macOS desktop notifications (#4)** — `desktop.py` gains a darwin backend
  behind a `sys.platform` check (the Linux `notify-send`/D-Bus path is unchanged):
  `available()` returns true when `osascript` is present, passive notifications use
  `osascript display notification`, and the `request(action="ask")` button
  round-trip uses `terminal-notifier` (or an `osascript` dialog fallback). Without
  it, urgent-to-human prompts silently no-op'd on macOS. Darwin path is unit-tested
  against mocked `osascript`/`terminal-notifier` output; not yet run on a real Mac.

## [0.1.5] - 2026-09-12

### Added

- **`cabildo doctor`** — one command that verifies the whole install and flags
  what won't work on this machine. It checks the six hooks are wired in
  `~/.claude/settings.json`, that the `cabildo` bus server is registered (user or
  project scope) and the per-repo `issues` server is wired where a `docs/issues/`
  exists, that the SQLite bus is reachable and in WAL, and whether a daemon is
  live. Crucially it also flags the platform features cabildo leans on that a
  given OS may lack — `/proc` process identity, `notify-send`, and
  `systemd --user` — so the macOS gaps surface at check time instead of as an
  agent that mysteriously never joins the bus. Exits non-zero only on a genuine
  break; platform gaps and a missing (optional) daemon are warnings.

## [0.1.4] - 2026-09-12

### Fixed

- **`show_issue` no longer overruns `SHOW_LIMIT`.** The truncation footer embeds
  the issue's absolute path, whose length is unbounded, so a long path pushed the
  output past the cap (a red suite on macOS, where pytest temp dirs live under
  `/private/var/folders/...`). The footer's rendered length, path included, is
  now budgeted against `SHOW_LIMIT`, so body + footer stays within the cap
  regardless of path length. (#1)

## [0.1.3] - 2026-09-12

### Added

- **`cabildo viewer`** — a read-only local web view of the bus (agents, locks,
  messages) served on `http://127.0.0.1:8787`, refreshing every 2s. Stdlib
  `http.server` only, binds to localhost, needs nothing running. It is the open
  counterpart of the hosted dashboard: the same glanceable view of your fleet,
  with no account and no server.

### Changed

- **The hosted backend split out.** The App Engine bus, the browser dashboard,
  and the machine runner moved to a separate private repo, `cabildo-backend`.
  cabildo is now self-contained: the local SQLite bus, the two MCP servers, the
  hooks, the `watch` TUI, and the new `viewer` all work with no network and no
  account. `cabildo sync` remains here as the optional one-way client that
  mirrors the local bus up to a hosted instance.
- **Repo aligned to the standard OSS template**: proto/moon-driven GitLab CI
  (one `moon run :ci`, `:release` on main via PyPI OIDC Trusted Publishing),
  `pre-commit`/`pre-push` git hooks, version-bump guards (`scripts/bump-version`,
  `check-version-bump`, `ci-version-guard`), ruff + pyright config, mkdocs +
  Pages, and `uv.lock`. `__version__` is now mirrored from `pyproject` and
  asserted by a test.

## [0.1.2]

- Baseline release on PyPI: the coordination bus (direct/topic messaging,
  presence, advisory locks), the `issues` MCP server, hook-level lock
  enforcement, the daemon (intent queue + desktop notifications), and
  `cabildo sync`.
