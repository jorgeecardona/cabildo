# Changelog

All notable changes to cabildo are documented here. Versioning is [SemVer](https://semver.org/).

## [Unreleased]

- Gateway: put several Bedrock keys in `~/.config/cabildo/keys.toml` under `[bedrock.keys]`. The
  upstream `[gateway.upstream.alpha]` uses the key `alpha`, with no `key` line.
- Gateway: tokens and estimated cost of today and 7 days for each upstream, in
  `cabildo gateway status`, the workspace's mode line and `cabildo stats`.

- `cabildo stats` prints the numbers of the workspace's mode line: each session's context, CPU and
  memory, plan limits, gateway, cost, intents and Jupyter runners. `--json` gives them as data.
- VS Code extension 0.0.5: each unread message shows when it was sent, and the status bar no
  longer counts you as an agent.
- VS Code extension 0.0.4: a Stats list in the side bar shows those numbers.

- Notebook cells tagged `replay` run again, after `startup`, on every new kernel cabildo makes for
  the notebook (a restart, or after a reboot). `jupyter_edit_cell` and `jupyter_insert_cell` take
  `tags`.
- `env_file = ".env"` in a runner's config loads secrets into the runner.
- Docs: a data analysis session from start to end, and git diffs of notebooks with jupytext.

- A busy session no longer shows a stack of identical "cabildo: 1 new message(s) from @daemon"
  prompts. The push plugin submits one wake prompt and holds the others until that turn ends; it
  then wakes again only for messages that are still unread.

- Workspace: with the gateway on, the mode line shows which upstream serves the shown
  conversation, and a table of upstreams with their requests, failovers and why one is set aside.
  A subscription limit at 100% shows no pace or ETA.

- Gateway, `bedrock-invoke`: old tool results are cleared when the prompt passes the trigger, and
  then pile up until it passes it again. Before, every request after that kept only the last 3.
  The results of the newest message are no longer cleared.

- VS Code extension: on the Visual Studio Marketplace as `cardona-oss.cabildo`.

- VS Code extension 0.0.3: installs in VS Code 1.108 and later (it asked for 1.120).

- VS Code extension: a cabildo side bar with three lists. Claude Sessions (start, open, end),
  Agents (send a message) and Unread Messages (read, reply, mark read), with the unread count on
  the cabildo icon.

- `cabildo attach` says why it stopped: the host stopped, or it closed the viewer. It exits 75
  instead of 0, and `--hold` keeps the message on screen until Enter. A viewer that was dropped for
  being too slow stayed connected and froze; it is now disconnected. Run `cabildo host stop` to
  start the new host (it ends the hosted sessions).

- VS Code extension: `Cabildo: New Claude Session in Folder` starts a hosted session and opens it.
  A tab whose connection ends stays open with the reason. The extension no longer imports a
  `cabildo` folder of the open workspace.

- Hosted sessions: a viewer (`cabildo attach`, a workspace pane, the VS Code tab) was
  disconnected 5 seconds after its last key. It now stays attached. Attaching again at the same
  window size showed a blank screen; Claude now redraws. Run `cabildo host stop` to start the new
  host (it ends the hosted sessions).

- A VS Code extension (`packages/vscode/`): the status bar shows the
  agent count and your unread count, and `Cabildo: Attach to Claude Session` opens a hosted
  session in a terminal tab. See `docs/vscode.md`.

- Agents edit the cells of a notebook that you have open, and you see it live: new MCP tools
  `jupyter_cells`, `jupyter_edit_cell`, `jupyter_insert_cell`, `jupyter_delete_cell` and
  `jupyter_run_cell`, `jupyter_run(add_cell=True)`, and `cabildo jupyter cells`. Runners install
  `jupyter-collaboration` (turn off with `collaboration = false`). An edit waits until you stop
  typing. The guard denies `Edit`, `Write` and `NotebookEdit` on a notebook that a live runner
  serves (`[jupyter] guard = false` turns it off).

- The daemon supervises Jupyter runners: `autostart = true` in `[jupyter.runners.<name>]` starts
  the runner and starts it again when it ends or after a reboot, a runner you stopped stays
  stopped, one that ends 5 times in an hour is left alone, and `memory_max = "6G"` pauses a
  runner over that memory. `python -m cabildo.daemon --no-jupyter` turns it off.

- `jupyter_run` returns plots as files: `images` lists each saved PNG with its path and size (an
  agent reads it with `Read`), `html` lists plotly figures, `full_text` is a file with the whole
  text when it was cut, and `execution_count` is the kernel's `In [n]`. New `jupyter_interrupt`
  and `jupyter_restart` tools, `cabildo jupyter interrupt|restart`, and `r` and `i` on the
  workspace runners screen.

- New MCP tool `clipboard_copy(text)` and command `cabildo clip [TEXT]` copy text to the system
  clipboard on Linux (`xclip`, `xsel`, `wl-copy`) and macOS (`pbcopy`).

- New MCP tools for an agent to run other Claudes: `sibling_start` (a first prompt, a folder, a
  permission mode), `sibling_send` (type into it and press Enter), `siblings`, `sibling_kill` and
  `host_stop`. The sibling is owned by the session host and is told who started it. At most 4
  run at once, and a sibling can start one more level, not a third. `cabildo host stop` does
  the same from the console.

- New MCP tool `sessions_move(id, to_dir)`: moves a conversation to another folder, so
  `claude --resume` finds it there. It refuses while a Claude runs it.

- A session started in the session host no longer inherits the variables of the Claude that
  started it (`CLAUDECODE`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ID` and the
  channel and token ones). Before, a session started from inside another Claude turned its
  transcript saving off and never showed in the list.

- New MCP tool `about`: the version of the server, the installed one, the daemon's and the
  session host's builds, the paths and sockets, how many sessions the host owns, the help
  topics, and a `restart` list with the command for each process that runs older than the
  install. It starts nothing. A session host that no longer has its socket file now stops
  by itself, so a host left behind by a deleted directory does not run for ever.

- A runner can use a uv project's environment: `project = "~/dir"` in `[jupyter.runners.<name>]`
  runs the kernel with the project's locked packages. `jupyter_run(notebook="a/b.ipynb")` and
  `cabildo jupyter run --notebook` run on that notebook's kernel session, so the notebook you open
  in JupyterLab shares the agent's variables. The notebook file is made when it is missing.

- Jupyter runners can be paused: `cabildo jupyter pause|resume NAME` and the `jupyter_pause` tool
  stop a runner and its kernels using the CPU and keep their memory. The workspace shows a
  `runners` group in the left panel (CPU and memory per runner) and a runners screen on `J` with
  stop, pause or resume, and open.

- Jupyter runners: `cabildo jupyter start|stop|status|open|run` and the MCP tools `jupyter_start`,
  `jupyter_run`, `jupyter_open`, `jupyter_list` and `jupyter_stop`. A session starts a Jupyter
  Server in a directory it chooses; agents run cells on its kernel; `open` gives a link that shows
  the same kernel in your browser. Packages, Python, environment and startup code are set in
  `[jupyter]` and `[jupyter.runners.<name>]`. See docs/jupyter.md.

- New MCP tool `sessions_focus(name)`: tell an agent "take me to xxx" and the running workspace
  opens that session and gives it the keys. `name` is an id, a robot name, an `@handle` or part
  of a title. It answers why when the workspace does not list the session or the name is
  ambiguous.

- Find a session by name: `sessions_list(query=...)`, `cabildo sessions list -q` and the workspace
  `q:` filter now match the robot name (`tiny-dynamo-616b8`), the `@handle` and the session id.
  `sessions_rename`, `sessions_tag`, `sessions_trash` and `sessions_read` accept a robot name or
  an `@handle` in place of an id.

- `[sessions] owner = "host"` in the workspace config makes a separate process, the session
  host, own each Claude's terminal. Quitting the workspace leaves the sessions running, a
  reload gives the ones it owns to the host without restarting them, and `cabildo attach <id>`
  shows one in any terminal. New commands: `cabildo host` (`list`, `new`, `kill`) and
  `cabildo attach`. See `docs/session-host.md`.

- The workspace spend table has a `7d share` row: each model's part of the last week's priced
  cost, in percent. The table shows on a plan login with `[workspace] spend = "always"` in
  `~/.cabildo/config.toml`; the default `auto` shows it only on an API key.

- A `bedrock-invoke` upstream no longer loses the clearing of old tool results that Claude Code
  asks for in `context_management`: the gateway applies the `clear_tool_uses` edit itself before
  it drops the field, so long conversations stay under Bedrock's window (#60).

- `cabildo service install` stops and removes the old `cabildo-gateway.service` (or
  `com.cabildo.gateway.plist`) that `service install --gateway` used to write, so it no longer
  holds the gateway port. `service uninstall` removes it too, and `service status` points it
  out. A unit that cabildo did not write is left alone (#58).

- The MCP servers no longer use the `mcp` package. A stdio server written in cabildo
  (`mcp_stdio.py`) replaces it, so an install drops `mcp`, `starlette`, `uvicorn`, `sse-starlette`
  and the packages only they needed. Tool names, arguments and results are the same. A tool
  result is now one compact line of JSON.

- The gateway log shows the upstream's error message on a 4xx or 5xx answer, on one line, cut to
  200 characters, so a 400 can be diagnosed without a separate reproduction.

- `browser_login` and the other 1Password reads use the service account token in
  `~/.config/cabildo/env` (`OP_SERVICE_ACCOUNT_TOKEN`) when it is there, so cabildo can read only
  the vaults of that service account (#59). A token line that cannot be used stops the call.

- Failover no longer breaks a conversation that has thinking on: the gateway removes thinking
  blocks another upstream issued, and retries once without them when an upstream refuses a
  signature. A `bedrock-invoke` upstream also drops a `temperature` other than 1 while thinking
  is on (#61).

- A `bedrock-invoke` upstream rewrites or drops the fields Bedrock refuses (`thinking.type
  enabled`, `context_management`, `service_tier`, `top_p` with `temperature`) and logs each change
  (#60). `drop_fields` lists more fields to drop for any `key` or `bedrock-invoke` upstream.

- The daemon saves the names you gave sessions (handles, roles, titles, groups, tags) to
  `names.json`, or to `[names] file`. `cabildo names export` writes it now, and
  `cabildo names import` puts the names back on a new machine without overwriting yours.

- The gateway runs inside the daemon, so there is one service. `cabildo service install --gateway`
  and the gateway unit are gone. `cabildo service reload` keeps open requests and hands the
  listening socket to the new process, so clients see no refused connection.
- `cabildo gateway enable` points every Claude Code session at the gateway through
  `~/.claude/settings.json`. `cabildo gateway disable` removes it.
- `docs/gateway.md` has a section on Anthropic's terms.

## [0.1.223] - 2026-11-20

Entries from 0.1.16 to 0.1.223 are listed together.

- The gateway has a third upstream kind, `bedrock-invoke`, for a Bedrock region that has no Mantle
  endpoint. It calls the Invoke API of `bedrock-runtime.<region>.amazonaws.com` with your Bedrock
  API key: it rewrites the path and body, decodes the AWS event stream into server-sent events,
  and turns Bedrock errors into Anthropic errors. `docs/gateway.md` says which kind a Bedrock
  region needs (#57).
- A new `help` MCP tool teaches an agent how to use cabildo. `help()` returns the start guide and
  the list of topics, `help("gateway")` one page, `help("tools")` every tool with its first line,
  and `help(query="push")` searches all pages. The pages are the files of `docs/`, bundled in the
  wheel. The new page `docs/using-cabildo.md` is the start guide.
- `cabildo service install --gateway` now writes only the gateway unit. Before, it also rewrote
  the daemon unit with default flags, which turned a daemon with `--allow-push` back to dry-run.
  `cabildo service status` lists the gateway when it is installed. The gateway default port is
  8788, because 8787 is the port of `cabildo viewer`.
- `cabildo gateway` is a local server for `ANTHROPIC_BASE_URL`. It sends each Claude Code request
  to your subscription first and, when that answers 429 or out of credit, to an API key or to
  Amazon Bedrock (Mantle, the Anthropic format), with no session restart. Upstreams are tried in
  the order of `~/.cabildo/config.toml`; a limit is remembered per model, with the reset time the
  upstream gives. `cabildo gateway use <name> --for 2h` picks an upstream yourself and
  `use auto` undoes it. `server_tools`, `models` and `drop_betas` keep a request away from an
  upstream that cannot serve it. A request from a web page is refused. `cabildo service install
  --gateway` runs it as a service, and `cabildo service reload` reads the config again. Not yet
  tried against a live Bedrock key.
- Agents started in a folder that is not a project (the home folder, the temp folder, or a folder
  that holds git repos) no longer share one `proj:` channel. They keep `general` and direct
  messages. `[project.<name>] isolated = false` brings the channel back. `cabildo doctor` warns
  when two folders carry one project name and says how to rename one (#26).
- An agent that only reads no longer blocks git in a shared checkout. The guard counts a peer
  only after it has written there: an edit tool, a git command that writes, or a shell command
  that writes a file. A session you open to ask questions never blocks another agent's squash,
  `git add -A` or checkout. Sessions that were already open count as before until they end (#56).
- `cabildo service reload` makes the daemon finish the intents it is running (a push in its gate,
  for example), take no new one, then start again on the current code in the same process
  (SIGHUP, `systemctl --user reload` too). `restart` still stops it at once and kills a running
  push. After this version, re-run `cabildo service install` once so the unit has `ExecReload`.
- `browser_login` needs no config: with no `site`, it fills from the 1Password Login item whose
  website URL has the page's origin (same `https`, host and port). Several matches are listed and
  `site=<title>` picks one. A `[browser.login.<site>]` table still wins for its origin. A locked
  or signed-out `op` now says how to fix it.
- You can rename a session and search sessions by title. `cabildo sessions rename <id> <name>`
  and the `sessions_rename` MCP tool set your own name (blank removes it; Claude's title shows
  again). In the workspace, `/` searches the titles, and `f` takes `q:<word>` next to `g:` and
  `t:`. Search matches your name and Claude's title, so a renamed session is still found by
  either. `sessions_list(query=...)` and `cabildo sessions list -q` already searched titles.
- The shared-checkout guard judges a git command by the folder it acts on (`cd <dir> &&`,
  `git -C <dir>`), not only by the folder the session started in. A commit in your own worktree is
  no longer denied because peers share the main checkout (#29).
- A push no longer fails because the tree holds uncommitted tracked changes, such as another
  agent's unfinished work in a shared checkout. The daemon makes its own checkout of your
  committed HEAD (kept between pushes, so a venv stays), rebases, runs the gate and pushes there,
  and leaves your tree alone. The result says `isolated: true`. Your branch still holds your
  commits under their pre-rebase ids; update it with `git pull --rebase --autostash` (#50).
- The daemon refuses a push when the same session already landed 10 pushes on the same target in
  the last 30 minutes, and tells the session why. This stops a `/loop` that pushes on every pass.
  Change it with `[push] max_per_window` and `window_minutes`; `max_per_window = 0` turns it off
  (#39).
- A push result now says what happened to the repo's pre-push hook: `pre_push` is `ran`,
  `skipped` or `none`, and the daemon's message warns when the hook was skipped, so `gate:
  passed` is not read as "everything ran" (#32).
- New MCP tools `record_minutes` and `read_minutes`: leave the written record of a session (a
  title and a markdown body, with optional cited decisions and tags) and read it back from any
  session of the project. Minutes are never edited: `revises` leaves a new revision and the first
  stays. `project_level=True` writes minutes that belong to no single session (#31).
- The browser server now says, in its first page result, when the saved login file is missing,
  empty or older than 30 days, and names the fix (`cabildo browser login`). New tool
  `browser_save_logins(domains)` merges the logins an agent browser holds for those domains into
  the file. `cabildo browser login --storage-state PATH` chooses where to save (#49).
- `join_topic` on a `shared` channel now says how many paired machines are reachable. When none
  is, it says so and points at `request(action="pair")` (#48). The `peers` tool lists the
  paired machines and their last sync.
- `list_topics` shows only topics that have a message or that you subscribe to, newest first, 50
  at a time. New arguments: `prefix`, `project`, `include_unused`, `min_messages`, `limit`,
  `offset`. Before, it returned all topics and could exceed the MCP result limit (#47).
- `cabildo browser login` turns on Chrome's "Continue where you left off" in the profile
  before it opens the window, so logins stored as session cookies (the Stripe dashboard) are
  still there when the cookies are read (#51). Not yet checked against a real Stripe login.
- `CronDelete` of a `cab-N` job also deletes the copy Claude Code restores when a session is
  resumed, so its ticks stop. `CronList` shows such a job once (#53). Reinstall the cabildo-push
  plugin to get this.
- An approved project `send` grant now lets the session that asked post, also after it moves to
  another project, and also when it named the project by its folder path. The refusal names the
  sender's own project (#52).
- The shared-checkout guard no longer denies `reset`, `rebase`, `merge`, `pull`, `cherry-pick`,
  `revert` and `am` just because another session is present. It denies them only when a tracked
  file has uncommitted changes (#55).
- A session that left a checkout stops blocking it: the tool hooks refresh the stored folder
  (#54).
- `browser_login` fills a login form from 1Password. List sites in `[browser.login.<site>]` in
  `~/.cabildo/config.toml`. The agent never sees the password, and the page must be on the
  site's origin. Each field target is optional, so a login that spans two pages (password, then
  one-time code) is two calls.
- `cabildo code` opens a chat with a small coding agent for a local model; `cabildo code "task"`
  runs one task and exits. Text streams as it is written, each tool call is shown, Ctrl-C stops
  the turn, `/clear` forgets the conversation, Ctrl-D leaves. It talks to any
  OpenAI-compatible endpoint (`--base-url`, default `http://localhost:8000/v1`; `--model`).
  Tools: read, edit, bash, grep. The agent joins the bus as client `local` and takes a
  `file:<path>` claim before each edit, so it cannot overwrite a file another agent holds.
  Claims are released when you leave.
  The agent reads the nearest `AGENTS.md` (from the working folder up to the project root,
  cut at 8000 characters) into its system prompt.
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
  now chosen from the repo's `[push].mode`. A `push` repo is told to `push` and that `push` moves work to `origin/<target>` and does NOT touch its
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

- **Peer-to-peer LAN sync (#7)**: `cabildo lan` shares a project's message bus
  with peers on the local network, with nothing leaving the subnet. Each peer
  UDP-broadcasts a beacon (`255.255.255.255`, so it never routes off-LAN) and
  serves its rows over TCP; peers discover each other and PULL, so the sync is
  bidirectional by both sides pulling and the serving side only ever reads. New
  messages are deduped by `guid`, stamped `provenance='lan'`, and the
  App Engine uplink now excludes `provenance='lan'`, so a LAN peer's messages never
  silently escape to the hosted mirror. Single-machine SQLite stays the default;
  this is opt-in. Flags: `-l/--loop`, `--interval`, `--port`, `--project`,
  `--secret` (an optional shared secret both peers must present). Stdlib only —
  no new runtime dependency. This first increment covers messages for one
  project; presence/claim sync and encrypted/authenticated pairing are the
  follow-up (the RTC-over-backbone transport). LAN v1 is trusted-LAN: the secret
  gates the pull handshake, but there is no transport encryption.
- **Global message `guid` (#7)**: `messages` gains a `guid` column (uuid4,
  additive migration that backfills existing rows and adds a unique index).
  `store.post_message` mints one per locally-born message and is now idempotent
  by guid (`ON CONFLICT DO NOTHING`); the cloud downlink and the LAN transport
  pass the original guid + `created_at` so the same message merges once across
  machines instead of re-inserting. This also closes a latent double-insert on a
  lost sync cursor.
- **Installer client selection + non-mutating preview (#12)**: `scripts/install.py`
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
- **Cross-process transport test (#12)**: `tests/test_transport_subprocess.py` runs
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

- **Codex delivery + cooperative coordination (#10)**: `cabildo codex` now also
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

- **Codex MCP setup (#9)**: `cabildo codex` wires Codex onto the same local bus
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

- **Agent instructions (#5)**: root `AGENTS.md` (Overview/Build/Test/Style/Gotchas)
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

- **Client-independent session identity and lifecycle (#6)**. The `sessions`
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

- **macOS service install via launchd (#5)**: `cabildo service` gains a launchd
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

- **macOS memory attribution (#3)**: `procs.py` gains darwin backends behind a
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

- **macOS process identity (#2, #11)**: `context.py`'s `_ppid`/`_argv` gain a
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

- **macOS desktop notifications (#4)**: `desktop.py` gains a darwin backend
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

- **`cabildo doctor`**: one command that verifies the whole install and flags
  what won't work on this machine. It checks the six hooks are wired in
  `~/.claude/settings.json`, that the `cabildo` bus server is registered (user or
  project scope) and the per-repo `issues` server is wired where a `docs/issues/`
  exists, that the SQLite bus is reachable and in WAL, and whether a daemon is
  live. It also flags the platform features cabildo leans on that a
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

- **`cabildo viewer`**: a read-only local web view of the bus (agents, locks,
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
