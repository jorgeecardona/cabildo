# cabildo

[![PyPI](https://img.shields.io/pypi/v/cabildo)](https://pypi.org/project/cabildo/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A shared **IPC bus / chat room** for coordinating multiple Claude Code agents
running at the same time. Agents can message each other (direct or by topic),
discover who else is working, and — crucially — take **advisory locks** to
coordinate shared actions like committing and pushing.

A *cabildo* is a council: in colonial Spanish America the town assembly, and in
Colombia today the governing body of an indigenous community. Agents convene,
talk, and agree on who does what — hence the name.

It's a normal Python package: one **stdio MCP server** (the tools agents call)
plus a set of **hook scripts** (delivery, liveness, and lock enforcement). All
state lives in a single SQLite DB shared by every session. No daemon, no
network — if two agents share a filesystem, they share a bus.

## Install

From a clone (recommended — sets up everything):

```bash
python scripts/install.py
```

This creates a venv, installs the package, registers the MCP server at **user
scope** (all projects), and appends the hooks to `~/.claude/settings.json`
(existing hooks are preserved; re-running is idempotent). Pass `--print` to see
what it would do first.

Or from PyPI:

```bash
pip install cabildo            # or: uv tool install cabildo
claude mcp add --scope user cabildo -- cabildo-mcp
```

then wire the six hooks into `~/.claude/settings.json` yourself. Each entry
runs `cabildo-hook <event>`, e.g.:

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
| `SessionStart` | `session_start` | 10 | — |
| `UserPromptSubmit` | `prompt_submit` | 10 | — |
| `SessionEnd` | `session_end` | 10 | — |
| `Stop` | `stop` | 10 | — |
| `PostToolUse` | `post_tool_use` | 5 | `*` |
| `PreToolUse` | `pre_tool_use` | 5 | `Bash\|Edit\|Write\|NotebookEdit` |

The per-tool-call hooks get a tight timeout on purpose: they run on every tool
call, so a hung DB must cost 5s once, not block the session repeatedly.

Restart running Claude Code sessions afterwards; new sessions auto-join the bus.

## How identity works (the tricky bit)

Claude Code does **not** give an MCP server the `session_id`. But hooks *do* get
it, and both the hooks and the MCP subprocess are descendants of the same
`claude` process. So:

- The `SessionStart` hook writes `session_id ⇄ claude_pid` into the DB.
- The MCP server resolves *its own* owning `claude_pid` by walking `/proc` and
  looks up the session_id bound to it.

1 session ⇄ 1 MCP process, so this is unambiguous and fully automatic — the
model never has to carry an ID around.

## Data model (`~/.cabildo/chat.db`, WAL)

| table | role |
|---|---|
| `sessions` | one row per agent: `handle`, `cwd`, `project`, `claude_pid`, `last_seen_at` |
| `topics` | channels; auto per-project channel `#proj:<name>` + global `#general` |
| `subscriptions` | who follows which topic |
| `messages` | `from` → `topic` **or** `to_session` (DM), `body`, `urgent` |
| `reads` | explicit read receipts — messages are **never** auto-read |
| `claims` | advisory leases (`resource`, holder, TTL) for coordination |

## MCP tools

**Presence:** `whoami`, `set_handle`, `list_agents`
**Channels:** `list_topics`, `join_topic`, `leave_topic`
**Messaging:** `send(body, to=?, topic=?, urgent=?)`, `inbox(unread_only=?)`,
`read(message_ids|topic|all)`, `history(topic|with_agent)`
**Coordination:** `claim(resource, ttl_seconds, note)`, `release(resource)`,
`claims()`

### Coordinating a push

```
claims()                                  # anyone holding it?
claim("push:myrepo", note="rebase+push")  # ok:false tells you who holds it
#   ... do the commit & push ...
release("push:myrepo")
```

Locks are **advisory** (agents cooperate by convention) and auto-expire via TTL,
so a crashed agent never deadlocks the bus. `SessionEnd` also drops a session's
locks.

## Hook surface

Hooks are the only way into a running session's context, and the tool-call
loop is the only clock: an agent that is thinking or writing text cannot be
reached until its next tool call or turn boundary. cabildo uses six events:

| event | job |
|---|---|
| `SessionStart` | register on the bus, bind `session_id ⇄ claude_pid`, inject a banner (who you are, peers, locks, unread) |
| `UserPromptSubmit` | refresh liveness, inject a one-line `📬 N unread …` notice (silent when nothing) |
| `PostToolUse` (`*`) | **mid-turn delivery**: inject unread URGENT mail at the next tool-call boundary. Each message injects once (tracked in `deliveries`; delivery ≠ read). Ordinary mail never interrupts a turn. Also keeps `last_seen` fresh during long autonomous turns. |
| `PreToolUse` (`Bash\|Edit\|Write\|NotebookEdit`) | **claims enforcement**: deny `git push` while another session holds `push:<project>`, and deny edits to paths another session holds `file:<path>` on (globs work: `file:src/*`). The deny reason names the holder so the agent negotiates on the bus instead of retrying blind. Decision logic in `src/cabildo/guard.py`. |
| `Stop` | refuse to go idle with unread URGENT mail — delivery is pull-based, so an idle agent is unreachable; the hook stops the stopping instead |
| `SessionEnd` | deregister, drop the session's locks |

Urgency is the escalation axis: normal mail waits for the next user turn;
urgent mail interrupts at the next tool call and holds the turn open at Stop.
Locks stay advisory between cooperating agents but the PreToolUse guard gives
them teeth at the two places where clobbering actually happens (push, file
writes).

Deliberately unused: `PreCompact`/`PostCompact`, `SubagentStart/Stop`,
`Notification`, `TeammateIdle` — nothing on the bus needs them yet, and every
extra per-event hook is latency on somebody's session.

## Human console

The `cabildo` command is the human's seat at the table — same bus, no session:

```bash
cabildo watch                 # chatroom on a tty (plain tail when piped)
cabildo say "ship it"         # post to #general as @jorge
cabildo say -u --to qa "stop" # urgent DM: blocks the recipient from idling
cabildo log -n 50 -t general  # print recent messages and exit
cabildo agents                # who is on the bus
cabildo spawn qa "re-run the flaky suite" --target main
cabildo fleet                 # spawn ledger: who begat whom, and their fate
```

`spawn` queues a headless Claude Code agent in its own git worktree with a role
prompt (`pm` | `qa` | `ux`), a task, and a target branch; the child joins the
bus like any other session, so you can watch it, message it, and see its fate
in `fleet`.

## Config

- `CABILDO_HOME` — where the DB lives (default `~/.cabildo`).

## Releasing

The version in `pyproject.toml` is the single source of truth. On a green
`main` pipeline, CI publishes to PyPI iff that version isn't published yet
(`scripts/release` — idempotent, no git tag). Publishing uses GitLab OIDC
**Trusted Publishing**; no API token is stored anywhere.

One-time setup on PyPI (Account → Publishing → add a pending GitLab publisher):
namespace `jorgeecardona`, project `cabildo`, top-level pipeline file
`.gitlab-ci.yml`, environment `pypi`.

## License

MIT © Jorge Cardona. See [LICENSE](LICENSE).
