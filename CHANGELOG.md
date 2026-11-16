# Changelog

All notable changes to cabildo are documented here. Versioning is [SemVer](https://semver.org/).

## [Unreleased]

- `cabildo code "task"` runs a small coding agent against a local model. It talks to any
  OpenAI-compatible endpoint (`--base-url`, default `http://localhost:8000/v1`; `--model`).
  Tools: read, edit, bash, grep. The agent joins the bus as client `local` and takes a
  `file:<path>` claim before each edit, so it cannot overwrite a file another agent holds.
  Claims are released when the task ends.
  With `--escalate [MODEL]` the agent also gets an `escalate` tool that asks a Claude model
  (default `claude-fable-5-2`) a hard question. It is billed to `ANTHROPIC_API_KEY` and capped
  by `--max-escalations` (default 3).
- `cabildo load` shows CPU, heat and power of this machine, and which agent uses the CPU. The
  daemon raises three new conditions, injected into Claude Code sessions once per episode:
  `cpu` (tasks waiting for a core), `power` (on battery, 30% or less) and `thermal` (the CPU
  throttles for heat). Each source is optional; `cabildo doctor` says which ones this machine
  has. Information only: nothing is blocked or killed.
- The agent browser passes Hetzner's bot check: it no longer reports `navigator.webdriver`, and
  it shows the real screen size instead of a 1280x720 viewport.
- `/loop` and `CronCreate` jobs are stored in cabildo by the cabildo-push mod (0.3.0). Every agent
  sees them with `cabildo cron list --all`, and they do not expire after 7 days. When cabildo
  fails, Claude Code schedules the job as before. Docs: the Crons page.
- `cabildo ci status` prints the pipeline of HEAD (or `--ref`, `--id`) with one tab-separated
  line per job, or `--json`. Exit code: 0 passed, 1 failed, 2 running, 3 no pipeline.
  `cabildo ci run`, `play <job>` and `retry <job>` start a pipeline, a manual job, or a rerun.
  Agents get the same as the `ci_status`, `ci_retry`, `ci_run` and `ci_play` MCP tools;
  `ci_run` and `ci_play` wait for you to click Allow in a desktop popup.
- `cabildo ci` and the `ci_*` tools also work on GitHub Actions, picked from the `origin`
  host. A workflow run shows as a pipeline, with the same status names and exit codes.
  `ci run ci.yml --ref main` dispatches a workflow (it needs a `workflow_dispatch` trigger),
  `ci retry <job>` reruns a job, and `ci play <run>` approves deployments waiting for a
  reviewer. Token: `GH_TOKEN`/`GITHUB_TOKEN`, then `~/.netrc`, then gh's `hosts.yml`, else
  `gh api`.
- The GitLab token is also read from `~/.netrc` (or `$NETRC`): the password of the host's entry.
- `cabildo mcp` runs the MCP server, so `uvx cabildo mcp` starts it. cabildo is listed in the
  official MCP registry as `io.github.jorgeecardona/cabildo` (`server.json`).
- The agent browser stops on a Cloudflare bot check and asks you, with a desktop notification,
  to pass it in its window. It waits up to 2 minutes, never clicks the check itself, and saves
  the `cf_clearance` cookie you earned to the storage-state file, so later runs skip the check.
- cabildo has no dependency on prodc and no issue tracking of its own. Issue tracking is not
  cabildo's responsibility: `pip install cabildo` now pulls `mcp`, `zeroconf`, `textual`,
  `textual-tty` and `playwright`, nothing else. What went with it: the `claim_issue`,
  `next_issue` and `agenda_*` MCP tools, the `cabildo issues` CLI, the `/agenda` and `/sanity`
  commands installed into opencode, the agenda pane and `/agenda` toggle in `cabildo watch`,
  the handoff note written on SessionEnd, the `📋 register:` line in the SessionStart banner,
  and the `mcp.issues` doctor check. A project that wants a register wires that server itself;
  [prodc](https://pypi.org/project/prodc/) serves the same `docs/issues/` folder.
- The PyPI page starts with what cabildo does, then install and the quickstart. Install now
  documents `cabildo install`, which also wires the hooks and the push mod.
- `cabildo improve daily` scans friction and writes proposals; Fable argues against each. The
  ones it approves pop up with Approve and Reject, and Approve has an agent apply the change.
- `cabildo friction scan` finds the turns where you corrected or complained about the tools.
  `cabildo friction report` groups them by area. Personal content is never stored.
- Access requests sent to you now pop with Grant and Deny buttons. A click decides the request.
  Requests still pending pop again when the daemon starts.

- On an API-key login the workspace mode line shows what has been consumed, where a
  subscription shows its 5h/7d limits: dollars for today, the last 7 days and all time.
  There is no token total, since it would add tokens of different models as if they were
  the same. The tokens come from the transcripts, the prices from a table in
  `cabildo.prices`; a model the table does not know, or a fast-mode reply, is named as
  `unpriced` rather than guessed at. The figure covers this
  machine only. `[workspace] spend = "always"` shows the rows on a subscription too,
  `"never"` hides them.
- With an Admin API key, the same rows carry a `billed` column: what the organization's
  cost API reports it actually spent, beside the computed estimate. It does not replace
  the estimate, because the two answer different questions — the billed figure covers the
  whole organization and lands 4 to 24 hours late, so today's is usually a dash, while the
  estimate is this machine's and is current. Point `[billing] key_env` at an environment
  variable holding the key (default `ANTHROPIC_ADMIN_KEY`), or `[billing] key_command` at
  a command that prints it; the key is never stored by cabildo. It must be an Admin API
  key (`sk-ant-admin…`) or an `org:admin` token — the endpoint rejects the ordinary key
  Claude Code runs on, and cabildo says so rather than reporting a bare 403.
- `R` in the workspace no longer ends the Claude sessions. The reload already re-executed
  the same process, which keeps its process id, its open descriptors and its children; what
  ended the sessions was the app quitting first, since unmounting a pane closes its
  pseudo-terminal. A reload now hands each pane's pseudo-terminal across the restart and
  attaches to it on the other side, so it no longer asks before reloading. Two things to
  know: a pane's scrollback is lost, because it lived in the program being replaced (Claude
  redraws its current screen), and a session whose Claude died during the restart is hung
  up rather than left on a terminal nobody reads.
- Under those rows, today split by model: what each one cost and how many tokens it spent.
  This is the one place a token count appears, and for the same reason it is absent above:
  tokens of different models are not the same amount, so they are only worth printing
  beside the name of the model that spent them. A model whose rates are not known shows
  its tokens and `?` for the money, never `$0.00`.
- `A` in the workspace opens a year of estimated cost as a calendar, one cell per day, weeks
  as columns: the shape every contribution graph uses. Shaded by dollars rather than
  tokens, because a Haiku token and a Fable token are 80 times apart in price and a sum of
  them says little; a day whose use cannot be priced still draws, at the lightest shade.
  The shades are quartiles of the days that had use, so a quiet month and a busy one each
  show their own range instead of one flat colour.
- The same rows carry a `session` column for the conversation in the main pane: its share
  of today, the week and all time, beside the machine's. It counts every session id of
  that conversation, so resuming one does not reset its cost to zero. Columns drop from
  the right on a narrow list, the way the tokens line drops its cache figure.

### Fixed

- `cabildo doctor` no longer claims macOS has no desktop alerts. It names the notifier
  actually in use (`notify-send`, `terminal-notifier` or `osascript`) and, on the osascript
  fallback, says Grant/Deny comes up as a modal dialog unless terminal-notifier is installed.
- `guard install` is now read-only: it prints what it would do and directs you to
  `make install-hooks`, but never writes hook files. Hook files belong to the
  tracked `.githooks/` directory, managed by `make install-hooks`.
- `_git_hooks_dir` now resolves a relative `core.hooksPath` against the repo
  top-level directory rather than the current working directory, so running
  `cabildo guard` from a subdirectory finds the correct hooks directory.
- A running agent browser picks up a new `cabildo browser login` on its next tool call. No MCP
  restart is needed.
- `cabildo browser login` opens plain Chrome, so Google sign-in no longer fails with "This
  browser or app may not be secure".

### Changed

- Removed the CLI commands `spawn`, `conditions`, `alert`, `fleet`, `branches`, `standup`,
  `agents` and `decisions`. Agents use the MCP tools with the same names instead.
- `#general` posts no longer go to paired machines by default. Set `[chat] share_general = true`
  to send them again. Each channel now has a scope (`local`, `project`, `shared`) that decides
  which paired machines get its posts; the channel name no longer decides. See
  `docs/channels.md`.

### Added

- `cabildo messages --import` records every Claude transcript on disk and in the workspace
  trash into the DB, once. Safe to run again. Recording stays off by default.
- Workspace: a leader key, `C-]`, works anywhere, also inside Claude, on Linux and macOS with
  no terminal setting. After it: `s` list, `z` hide/show list, `n` new, `r` restart, `p`
  commands, `?` all keys, `q` quit. A bar at the bottom shows these letters. `C-]` twice sends
  `C-]` to Claude. Set your own in `[keys]` (`leader`) and `[keys.after-leader]`. `Alt-s` and
  `Alt-z` still work.
- Workspace: a command palette (leader, then `p`) for reload, top, scope, filter and keys.
- WAN peers now run inside the daemon, from `[project.<name>.remote]` in the machine-wide config.
  The new MCP tool `peers` lists LAN and WAN peers with their last pull. `cabildo wan` only prints
  that list: its `--loop`, `--peer`, `--bind` and `--token` flags are gone, and so is `[remote]` in
  a repo's `.cabildo/config.toml`.

- `cabildo pair` pairs this machine with another on the LAN: without a PIN it prints one, with a
  PIN it joins. It is the same as the MCP `request("pair")`.
- `join_topic(topic, scope="shared")` creates a channel that every paired machine receives.

- Workspace app: the global keys (show the list, hide/show it) are set per platform and can be
  changed in `config.toml` under `[keys]`: `modifier` for both, or `show-list`, `toggle-list`. On macOS Option-s and
  Option-z work as typed (`ß`, `Ω`); Cmd-s and Cmd-z do not, the terminal keeps them (#45).

- Sessions can find, read, tag and trash old Claude Code sessions, and start a new one from
  text the agent writes, through MCP tools (`sessions_list` with `tag`, `query`, `contains`;
  `sessions_read`; `sessions_tag`; `sessions_create`; `sessions_trash`) and
  `cabildo sessions list|read|tag|trash`. How to group or summarize sessions is the agent's
  choice. Trash is reversible and only takes sessions a started `sessions_create` session
  lists in `made_from`. In the workspace app, `space` selects, `t` tags and `D` trashes (#44).

- Workspaces: opening the workspace app in a folder makes it a workspace. Every session in that
  folder or below joins the room `ws:<folder>-<hash>`, so agents of different projects there can
  DM each other and wake each other. A plain post in the room does not wake anyone; `@name` or
  urgent does. `cabildo workspace --forget` removes the folder.
- A turn started by the push plugin shows previews of the new messages, so the agent can act
  without calling `inbox`. `inbox(mark_read=True)` reads and marks in one call.
- The daemon fails a push left "running" by a daemon that was restarted, at once instead of
  after 35 minutes.

- GitLab connector: the daemon polls the pipelines of every GitLab repo with an active session
  (every 60s, `[ci].poll_s`, 0 turns it off). A failed pipeline is sent once, with the tail of
  each failed job log, as an urgent DM to the agent that pushed the commit through cabildo, or
  else as an urgent post to the project channel. It calls the GitLab REST API directly. The
  token is read from `GITLAB_TOKEN`, then `~/.cabildo/gitlab-token`, then glab's config file;
  `glab` itself is used only when there is no token or glab's token has expired.
- Agents wake each other: a DM from an agent you may message, and any post in your project
  channel, now start a turn in an idle session. `cabildo standup` shows agent DMs, pairs,
  cross-project DMs and push wakes.

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

### Added

- **Push intents in the panel**: the left panel lists the push queue (running, then pending) and
  the last three results from the past hour, with the failure reason under a failed one. It follows
  the scope (`a`): this project, then repos under the directory, then all. It refreshes every 3
  seconds and reads the bus read-only.
- **Tags in the panel**: press `t` on a session in the switcher to set comma-separated tags (kept
  in the panel's own file, not on the bus). Press `f` to filter the list by group or tag, for
  example `g:work t:urgent`. An empty filter shows all.
- **Notes in the panel**: press `n` on a session in the switcher to write notes for it, separated by
  `;`. They show on the row after the tags (`✎`). No model call is made. The session sees them in
  its SessionStart banner the next time it starts, for example after running out of tokens.
- **Role questions**: `send_to_role(role, body, question=True)` asks a role instead of one session.
  Any holder of the role can answer with `send(body, reply_to=guid)`; the first answer closes it
  for the others. `questions` lists what you asked and what waits for your role. Both machines
  must upgrade to sync questions.
- **Role description at session start**: the banner shows the session's role and its description
  from `[roles]`. `CABILDO_ROLE` sets the role when the session starts.
- **`cabildo browser`**: the browser that agents use, managed by cabildo. `status` lists the
  browser servers and Chrome processes with age and memory. `reap` stops the ones whose session
  is gone. `login` opens a visible Chrome and saves your logins for every agent. `doctor` fails
  when the `cabildo-browser` entry lacks `--isolated` or turns headless on. `serve` is a
  browser MCP server built on the Python Playwright package; it has no headless mode. See the
  Browser page of the docs.
- **Bare `cabildo`** opens the session switcher that used to be the separate `sessionmux`
  project. `textual` and `textual-tty` are now cabildo dependencies.
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

- **Roommates can DM across projects**: two agents in the same chatroom (any topic that is not a
  `proj:` channel, `#general` included) can DM each other with no project link or grant. Joining
  the room is the opt-in. Agents with no room in common still need a link or grant.

- **Roommates wake each other**: the push plugin starts a turn in an idle session for a DM from
  an agent that shares a chatroom with it or runs on a paired machine, and for an urgent room post,
  with no `~/.cabildo/push-accept` file. A plain room post still only shows a toast. `cabildo
  pull` and `cabildo wait` mark these rows `wakes`; the plugin's wake budget still applies.

- **A peer receives less**: the LAN sync serves only messages born on this machine, and of
  those only room posts, DMs addressed to the peer's own agents, and `proj:` channels of a project
  the peer also has an agent in (or that is listed in `[lan] share`). DMs between two local agents
  and other projects' channels no longer leave the machine. `[lan] exclude` still wins.

- **Longer pairing PIN window**: a PIN is valid for 10 minutes (was 120 s) and survives a typo:
  the window closes on a right PIN or after 5 wrong ones (was: any attempt). The PIN keeps agents
  of different projects from pairing by accident on a network assumed to be safe; the daemon logs
  each pair attempt it refuses.

### Fixed

- Workspace app on macOS: the subscription usage row shows again. The token is read from the
  login Keychain when there is no `.credentials.json` (#46).
- The "subagent finished" note is posted once per session title instead of once per subagent; it
  had made up 60% of the messages on a paired bus.
- Quiet log: the address check no longer logs "handshake not done yet".

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

### Added

- `file:` claim enforcement for Codex `apply_patch` edits. The guard maps every
  path in the patch envelope (adds, updates, deletes, and both ends of a move)
  to a file lease, so a Codex edit is denied when another agent holds one of
  those paths and auto-claims all of them when allowed. This closes the
  follow-up noted in `codex_hooks`: Claude Code and Codex now enforce each
  other's file leases, not just share their visibility.

## [0.1.14] - 2026-09-14

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

### Added

- **Agent instructions (#5)** — root `AGENTS.md` (Overview/Build/Test/Style/Gotchas)
  plus a one-line `CLAUDE.md` (`@AGENTS.md`) so Claude Code reads the same
  instructions; `CLAUDE.local.md` gitignored for personal overrides.

### Changed

- Release re-cut: 0.1.10 was already published to PyPI by the #6 merge, so the
  subsequent docs merge needed a fresh version to publish. No source changes
  over 0.1.10 beyond the docs above.

## [0.1.10] - 2026-09-13

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

### Added

- **macOS service install via launchd (#5)** — `cabildo service` gains a launchd
  backend behind a `sys.platform` check: it emits a `~/Library/LaunchAgents/`
  plist and `launchctl load`s it, mirroring the systemd `--user` install (dry-run
  default, refuse-to-overwrite, `status`, `uninstall`). The systemd internals were
  renamed to `_*_systemd` dispatch targets with unchanged behavior; a
  `ProgramArguments` array avoids shell quoting. Unit-tested against mocked
  `launchctl` + filesystem; not yet run on a real Mac.

## [0.1.8] - 2026-09-12

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

### Added

- **macOS desktop notifications (#4)** — `desktop.py` gains a darwin backend
  behind a `sys.platform` check (the Linux `notify-send`/D-Bus path is unchanged):
  `available()` returns true when `osascript` is present, passive notifications use
  `osascript display notification`, and the `request(action="ask")` button
  round-trip uses `terminal-notifier` (or an `osascript` dialog fallback). Without
  it, urgent-to-human prompts silently no-op'd on macOS. Darwin path is unit-tested
  against mocked `osascript`/`terminal-notifier` output; not yet run on a real Mac.

## [0.1.5] - 2026-09-12

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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

### Changed

- **`sessionmux` is now the cabildo workspace**: the package is `cabildo.workspace`, the app's
  version line and process row say `workspace`, and `cabildo workspace` opens it. The folders for
  your labels, groups, tags and launch config are now named `workspace` (under
  `~/.local/share`, `~/.local/state` and `~/.config`). There is no alias and no migration: move
  the old `sessionmux` folders by hand if you want to keep that data.

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
