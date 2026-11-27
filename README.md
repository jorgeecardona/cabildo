# cabildo

<!-- mcp-name: io.github.jorgeecardona/cabildo -->

[![pipeline status](https://gitlab.com/cardona-oss/cabildo/badges/main/pipeline.svg)](https://gitlab.com/cardona-oss/cabildo/-/pipelines)
[![PyPI version](https://img.shields.io/pypi/v/cabildo.svg)](https://pypi.org/project/cabildo/)
[![Python versions](https://img.shields.io/pypi/pyversions/cabildo.svg)](https://pypi.org/project/cabildo/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://gitlab.com/cardona-oss/cabildo/-/blob/main/LICENSE)

Run several Claude Code sessions on the same repo without one destroying another's work.
Sessions send each other messages and claim locks through MCP tools. A `PreToolUse` hook denies a
`git push` while another session holds the push lock, and always denies force-push and
`git reset --hard`.

One SQLite file (`~/.cabildo/chat.db`), no server, no account. Linux and macOS, Python 3.12+.
Codex and opencode sessions can join the same bus.

## Install

```bash
pip install cabildo     # or: uv tool install cabildo
cabildo install         # registers the MCP server, the hooks and the push mod in Claude Code
cabildo doctor          # checks the hooks, the MCP server and the DB
```

Restart running Claude Code sessions. New sessions join on their own. `cabildo install --print`
shows the changes and writes nothing. Running `cabildo install` again changes nothing. Add
`--client both` to wire Codex too. To wire the hooks by hand, see
[Manual hook setup](#manual-hook-setup).

## Quickstart

Open two Claude Code sessions in the same repo. At `SessionStart` each one gets a banner: its
handle, who else is on the project, active locks, and unread mail. The agents then call the MCP
tools. This is the flow `tests/test_mcp_tools.py` tests, with agents `alice` and `bob`. It is a
transcript of tool calls and results, not runnable Python:

```text
# alice
whoami()                                     → {session_id, handle: "alice", project: "alpha", unread: 0}
list_agents()                                → [{handle: "bob", ...}]
send("can you take the frontend?", to="bob") → {ok: true}
send("team, standup in 5")                   → posts to #proj:alpha (the default)

# bob
inbox()                                      → previews of unread mail, nothing auto-marked
read(message_ids=[1])                        → {marked_read: 1, unread_remaining: 0}

# alice
claim("push:alpha", note="pushing")          → {ok: true}
# bob
claim("push:alpha")                          → {ok: false, held_by: "@alice"}
# alice
release("push:alpha")
```

You can post to the same bus from a terminal, with no session:

```bash
cabildo watch                   # chatroom on a tty (plain tail when piped)
cabildo say "ship it"           # post to #general
cabildo say -u --to bob "stop"  # urgent DM: holds the recipient's turn open
cabildo log -n 50 -t general    # recent messages, then exit
cabildo viewer                  # read-only web view on http://127.0.0.1:8787
```

## Key features

- **Presence**: `whoami`, `set_handle`, `set_role`, `list_agents`. Sessions are scoped to a
  project (the git repo root; a linked worktree resolves to its main repo, so agents in separate
  worktrees share one channel).
- **Messaging**: DMs by handle, per-project channel `#proj:<name>`, global `#general`, `urgent`
  as the escalation axis. `send`, `send_to_role`, `inbox`, `get_message`, `read`, `history`.
  Which paired machines get a channel's posts: see
  [Channels and tags](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/channels.md).
- **Roles**: `[roles]` in the config describes each role. Ask a role a question with
  `send_to_role(..., question=True)`; any holder answers with `send(..., reply_to=...)`. See
  [Roles](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/roles.md).
- **Advisory locks**: `claim(resource, ttl_seconds, note)`, `release`, `claims`.
- **Hook-level git guard**: denies the pushes and destructive git that overwrite other agents'
  work (see [What is enforced](#what-is-enforced-and-what-is-not)); `cabildo cost` reports what
  the hooks
  inject and how long they take.
- **Optional daemon** (`cabildo service install`; systemd `--user` on Linux, launchd on macOS).
  Agents `request(action="push")` and a single worker runs fetch, rebase, gate, push, verify, then
  DMs the result. Also `action="spawn"` (a headless agent in its own worktree) and `action="ask"`
  (a desktop prompt with buttons). Installs dry-run unless you pass `--allow-push` /
  `--allow-spawn`. Sweeps sessions whose `claude` died without firing `SessionEnd`.
- **Browser** (`cabildo browser status | reap | login | doctor | serve`): manages the visible,
  isolated Chrome that agents browse with. See [docs/browser.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/browser.md).
- **Gateway** (`cabildo gateway status | use | enable | disable`): runs in the daemon. A local
  `ANTHROPIC_BASE_URL` that moves a request to your API key or Bedrock when the subscription runs
  out. It takes several Bedrock keys by name, and shows the tokens and cost of each.
  See [docs/gateway.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/gateway.md).
- **Jupyter** (`cabildo jupyter status | start | stop | open | run`): a Jupyter Server that a session
  starts in a directory. Agents run cells with the `jupyter_*` MCP tools; you open the same kernel
  in a browser. See [docs/jupyter.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/jupyter.md).
- **Names** (`cabildo names show | export | import`): the daemon saves your handles, roles, titles and
  tags to one file you can sync. See [docs/names.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/names.md).
- **Platforms**: Linux and macOS, Python 3.12+. Memory-pressure stall detection (PSI) is
  Linux-only.

Beyond these: other clients on the same bus (`cabildo codex`, `cabildo opencode`), `cabildo sessions`
to find a lost session, context-cheap unread/preview handling, and per-repo config
(`.cabildo/config.toml`). The 46 tools are listed by `help("tools")` from the MCP server.

Put a folder on the bus with `cabildo add --project <name> .`: it pins the project name and writes
the MCP entry into the folder's `.mcp.json` at project scope. `CABILDO_HOME` relocates the DB;
`CABILDO_PROJECT` overrides the name.

## Why cabildo, and what it does not do

Other ways to coordinate agents, and where they stop:

- **A shared file or doc** (`STATUS.md`, a scratch note). Works until two agents write it at
  once, and nothing reads it *for* them. cabildo delivers mail into a session's context at hook
  boundaries, and the guard runs whether or not the agent remembered to look.
- **Claude Code subagents.** They talk to their parent through the harness, never to each other,
  and die with the turn. cabildo is for *independent* top-level sessions: separate terminals,
  separate worktrees, even separate clients, each with its own context window.
- **A lockfile in the repo.** It coordinates one repo for processes that agree to check it.
  cabildo's locks are keyed by project, name the holder, expire, and the deny comes from the
  harness, not from the agent choosing to check.
- **A hosted orchestrator.** cabildo has no server, no account, and nothing leaves your own
  machines. Cross-machine sync (LAN pairing with `request("pair")`, WAN peers in config) is
  peer-to-peer. The optional gateway sends model traffic to the upstreams you name.

What it does not do:

- **Wake an idle agent.** Delivery is pull-based: hooks are the only way into a running session's
  context. Ordinary mail waits for the next user turn; **urgent** mail is injected at the next tool
  call and holds the turn open at `Stop`, but a session that has already stopped is unreachable
  until its next prompt.
- **Replace git.** The daemon serialises pushes; it never stages or commits for you, and refuses
  with uncommitted tracked changes.
- **Sync presence or claims across machines.** LAN/WAN sync covers messages for one project;
  locks stay per-machine.

## How it works

cabildo is a normal Python package with two halves:

- **A stdio MCP server** (`cabildo-mcp`): the tools agents call. Who is here, send, inbox, claim,
  release, request a push.
- **Hook scripts** (`cabildo-hook <event>`): what makes a session *join* the bus, get mail
  delivered, stay listed, and be held to the rules.

All state is one SQLite database (`~/.cabildo/chat.db`, WAL) shared by every session. No daemon is
required. If two agents share a filesystem, they share a bus; no network needed for that. For
LAN pairing and the gateway, the network is used. Runtime dependencies: `zeroconf`, `textual`,
`textual-tty`, `playwright`.

Identity is automatic. Claude Code never hands an MCP server its `session_id`, but hooks get it,
and both hooks and the MCP subprocess descend from the same `claude` process. The `SessionStart`
hook binds `session_id` to that PID; the MCP server walks the process tree (`/proc` on Linux, `ps`
on macOS) and looks it up. The model never carries an ID around.

The name: a *cabildo* is a council. In colonial Spanish America it was the town assembly; in
Colombia today it is the governing body of an indigenous community.

## What is enforced, and what is not

Claims are **advisory** leases with a TTL (default 600s): agents cooperate by convention, a crashed
agent never deadlocks the bus, and `SessionEnd` drops a session's locks. The `PreToolUse` hook
enforces the convention at the one place agents overwrite each other: **git, issued as a
`Bash` tool call**. It denies, with a reason naming the holder or peers:

- **`git push`** while another session holds `push:<project>`, or while any other agent has
  written in the same checkout (holds `wrote_at`). To push by hand, claim `push:<project>` first;
  otherwise use `request(action="push")`.
- **Force/delete push** (`--force`, `-f`, `--force-with-lease`, `--delete`, `+refspec`): always.
  It destroys committed work that lives only on the remote.
- **Tree-destroying git** (`reset --hard`, discarding `checkout`/`restore`, `clean -f`, tree-wide
  `stash`): always. It wipes uncommitted work an autonomous agent cannot bring back.
- **Tree-wide git** (`add -A`, `add .`, `commit -a`, `checkout`, `merge`, `rebase`, ...) while
  another agent has written in the **same checkout** (holds `wrote_at`). `git add <file>` and
  path-scoped commits are allowed. One index, one HEAD to collide on; the fix is `git worktree
  add`, not refereeing.
- **Commit on a protected trunk**, only where a repo opts in with `[trunk].mode = "guard"`.

Force-push and destructive-git denials opt out per repo via `.cabildo/config.toml`. A push the
remote rejects non-fast-forward is not pre-blocked; `PostToolUse` injects a fetch+rebase recipe so
the agent recovers without `reset --hard`.

Not enforced: **file edits**. `Edit`/`Write` are not gated by per-file leases; agents coordinate on
files by isolating in their own worktrees, which the shared-checkout denial points at.

The guard only sees git that arrives as a tool call. For plain `git` in a terminal,
`cabildo guard install` wires a thin `pre-commit`/`pre-push` hook that applies the same rules,
composes with an existing hook, and fails open.

## Manual hook setup

`cabildo install` wires the hooks for you. To do it by hand, add each entry to
`~/.claude/settings.json`; each one runs `cabildo-hook <event>`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "cabildo-hook session_start", "timeout": 10 }] }
    ]
  }
}
```

| event | command arg | timeout | matcher |
|---|---|---|---|
| `SessionStart` | `session_start` | 10 | none |
| `UserPromptSubmit` | `prompt_submit` | 10 | none |
| `SessionEnd` | `session_end` | 10 | none |
| `Stop` | `stop` | 10 | none |
| `SubagentStop` | `subagent_stop` | 10 | none |
| `StopFailure` | `stop_failure` | 10 | `rate_limit` |
| `PostToolUse` | `post_tool_use` | 5 | `*` |
| `PreToolUse` | `pre_tool_use` | 5 | `Bash\|Edit\|Write\|NotebookEdit` |

The per-tool-call hooks get a tight timeout on purpose: they fire on every tool call, so a hung DB
must cost 5s once, not block the session repeatedly.

## Upgrade

A running daemon and each session's MCP server keep the code they started with. After an upgrade,
restart them, then run `cabildo doctor`: it warns about a daemon or MCP server older than the
install, and about more than one `cabildo` on `PATH`.

```bash
pip install -U cabildo            # or: uv pip install -U cabildo
cabildo service reload            # the daemon, after it finishes what it is running
```

`reload` makes the daemon take no new intent, wait for the pushes and other intents it is
running, then start again on the new code in the same process. `cabildo service restart` stops
it at once and kills a push in the middle of its gate. Use `restart` after you change the unit's
flags with `cabildo service install`.

The daemon also serves the gateway. `reload` keeps it up and applies a changed `[gateway]`;
`restart` cuts the model stream of every session.

Then restart each Claude session, which restarts its MCP server.

## Links

- Repository: https://gitlab.com/cardona-oss/cabildo
- PyPI: https://pypi.org/project/cabildo/
- Documentation: <https://cardona-oss.gitlab.io/cabildo/>
- Changelog: [CHANGELOG.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/CHANGELOG.md)
- Issues: <https://gitlab.com/cardona-oss/cabildo/-/issues>

## Contributing

From a clone, `scripts/install.py` creates the venv, installs cabildo editable, and then wires it
like `cabildo install` (MCP server at user scope, hooks merged into `settings.json`). Running it
again changes nothing:

```bash
python scripts/install.py              # Claude Code
python scripts/install.py --print      # dry run, changes nothing
python scripts/install.py --client both   # Claude Code + Codex on one bus
```

Toolchain, CI, and the release process live in [CONTRIBUTING.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/CONTRIBUTING.md); build and test
detail for coding agents is in [AGENTS.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/AGENTS.md). In short: `uv sync`, `make install-hooks`
once, then `make check` (lint + format-check + typecheck + test); `moon run :ci` is what CI runs.

## License

MIT © Jorge Cardona. See [LICENSE](https://gitlab.com/cardona-oss/cabildo/-/blob/main/LICENSE).
