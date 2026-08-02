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

A session's **project** is its git repo root. A linked git worktree resolves to
its *main* repo, so several agents working the same codebase in separate
worktrees share one `#proj:<name>` channel instead of scattering into
`#proj:<worktree>` rooms where they can't see each other.

## Data model (`~/.cabildo/chat.db`, WAL)

| table | role |
|---|---|
| `sessions` | one row per agent: `handle`, `cwd`, `project`, `claude_pid`, `last_seen_at` |
| `topics` | channels; auto per-project channel `#proj:<name>` + global `#general` |
| `subscriptions` | who follows which topic |
| `messages` | `from` → `topic` **or** `to_session` (DM), `body`, `urgent` |
| `reads` | explicit read receipts — messages are **never** auto-read |
| `claims` | advisory leases (`resource`, holder, TTL) for coordination |
| `hook_metrics` | cost telemetry: injected chars + latency per hook fire (see `cabildo cost`) |

## MCP tools

**Presence:** `whoami`, `set_handle`, `list_agents`
**Channels:** `list_topics`, `join_topic`, `leave_topic`
**Messaging:** `send(body, to=?, topic=?, urgent=?)`, `inbox(unread_only=?, full=?)`,
`get_message(id)`, `read(message_ids|topic|all)`, `history(topic|with_agent)`
**Coordination:** `claim(resource, ttl_seconds, note)`, `release(resource)`,
`claims()`

### Keeping the inbox cheap on context

The bus carries long messages (status reports run ~2k chars), so a naive inbox
is the thing that blows up a session's token budget. Two guards:

- **Unread starts when you join.** Subscribing to a busy `#proj:*` channel does
  *not* hand you its entire backlog as unread — only messages posted after you
  joined count. A fresh agent starts at zero, not at "100 unread ≈ 50k tokens".
- **`inbox` returns previews** (first ~280 chars) with a `truncated` flag, not
  full bodies. Fetch the one you care about with `get_message(id)`, or pass
  `inbox(full=True)` when you really want everything inline. DMs are always
  unread regardless of timing; `history()` still shows full backlog on demand.

### Coordinating a push

**While another agent is live on the project, `git push` is denied.** Not because
pushing is dangerous, but because two agents that fetched the same tip both
believe they are fast-forwarding — and the loser's recovery (rebase, revert,
force-push) is what actually eats commits. So commit, then queue it:

```
request(action="land", params={"cwd": "<your tree>", "target": "main"})
```

The daemon runs one intent at a time: fetch → rebase → gate → push → verify
against the remote, then DMs you the result. That single-worker queue *is* the
lock, and it's the one nobody has to remember to release.

Blanket staging (`git add -A`, `git add .`, `git commit -a`) is denied too, but
only when another agent shares your **exact checkout** — it would sweep their
half-written files into your commit. Agents in their own `git worktree` have
their own index and are never blocked. Stage explicit paths, or isolate first.

The escape hatch is deliberate and explicit:

```
claims()                                  # anyone holding it?
claim("push:myrepo", note="rebase+push")  # ok:false tells you who holds it
#   ... commit & push by hand: peers are now denied, the rebase is yours ...
release("push:myrepo")
```

Locks are **advisory** (agents cooperate by convention) and auto-expire via TTL,
so a crashed agent never deadlocks the bus. `SessionEnd` also drops a session's
locks. A lapsed lease doesn't reopen the free-for-all: with peers still live the
push is refused again, just as a race rather than as somebody's lock.

### What the daemon will and won't land

`land` is a request, and the daemon judges it before acting:

- **Uncommitted *tracked* changes block it.** The daemon lands commits; it never
  stages or commits for you. Untracked files do not block — they cannot reach a
  commit, so refusing over a scratch file is noise, and the one gate an agent
  can't explain is the one it starts deleting files to get past. The step log
  says how many were ignored. When the only dirty file is Claude Code's own
  `.claude/settings.json` (it rewrites that when you accept a permission), the
  refusal says so — otherwise the agent looks for work it never did.
- **Stale requests are refused, not run** (`--max-intent-age`, default 2h). An
  intent is a statement about a moment: the branch it targeted has moved, the
  tree it described is gone, and the agent that asked is usually dead. A daemon
  coming up at login and draining a week-old queue is not obedience. Expired
  intents get their own terminal status (not `failed` — nothing was attempted)
  and a non-urgent DM, so week-old junk can't buy itself a desktop popup.
- **A push it can't verify is never reported as landed** — and a push it can't
  *see* is never reported as failed either. See `tests/test_land_verification.py`.

## Hook surface

Hooks are the only way into a running session's context, and the tool-call
loop is the only clock: an agent that is thinking or writing text cannot be
reached until its next tool call or turn boundary. cabildo uses six events:

| event | job |
|---|---|
| `SessionStart` | register on the bus, bind `session_id ⇄ claude_pid`, inject a banner (who you are, peers, locks, unread) |
| `UserPromptSubmit` | refresh liveness, inject a one-line `📬 N unread …` notice (silent when nothing) |
| `PostToolUse` (`*`) | **mid-turn delivery**: inject unread URGENT mail at the next tool-call boundary. Each message injects once (tracked in `deliveries`; delivery ≠ read). Ordinary mail never interrupts a turn. Also keeps `last_seen` fresh during long autonomous turns. |
| `PreToolUse` (`Bash\|Edit\|Write\|NotebookEdit`) | **claims enforcement, with teeth**: (a) **auto-claim on edit** — an allowed Edit/Write silently takes a short lease (`file:<path>`, 10-min TTL, refreshed per edit) so the *next* agent to touch that file is denied, without anyone remembering to `claim`; (b) deny `git push` while another session holds `push:<project>` **or** while any other agent is simply live on the project, pointing at `request land` (a lease you hold yourself is the escape hatch); (c) deny blanket staging (`add -A`, `add .`, `commit -a`) when another agent shares the same *checkout* — worktrees have their own index and are exempt; (d) deny tree-destroying git (`reset --hard`, discarding `checkout`/`restore`, `clean -f`, `stash`) while any other agent is active on the project, since those wipe the whole shared tree's uncommitted work. Deny reasons name the holder/peers so the agent negotiates on the bus. Logic in `src/cabildo/guard.py`. |
| `Stop` | refuse to go idle with unread URGENT mail — delivery is pull-based, so an idle agent is unreachable; the hook stops the stopping instead |
| `SessionEnd` | deregister, drop the session's locks |

A killed terminal or crashed `claude` never fires `SessionEnd`, so the daemon
also sweeps (~every 60s) for `active` sessions whose `claude_pid` no longer
resolves to a live process and ends them — otherwise zombie rows accumulate
unread forever and clutter `list_agents`.

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
cabildo cost -d 7 --rate 3    # what running cabildo cost you over 7 days
cabildo add --project paas .  # put this folder on the bus (see below)
cabildo service install       # run the daemon in the background (see below)
```

`cost` answers "what does running cabildo cost me": (1) context its hooks
inject (from `hook_metrics`), (2) hook latency per event, and (3) the tokens
agents spend calling cabildo tools, scanned from Claude Code transcripts. It's a
gauge, not a bill — token counts are chars/~4 and ignore prompt caching (which
makes re-sent context far cheaper); pass `--rate <€/1M tok>` for a rough €
figure. `hook_metrics` only fills once the instrumented hooks are installed.

`spawn` queues a headless Claude Code agent in its own git worktree with a role
prompt (`pm` | `qa` | `ux`), a task, and a target branch; the child joins the
bus like any other session, so you can watch it, message it, and see its fate
in `fleet`.

## Adding a folder

A project is a git repo, named after its directory. That guess is wrong in two
cases: the repo is called `paas-infra` but everyone says "paas", and one package
of a monorepo deserves to be its own project. `cabildo add` pins the answer:

```bash
cabildo add --project paas .          # this repo IS "paas", for every session opened here
cabildo add --exact -p api packages/api   # one folder of a monorepo, as its own project
cabildo add --uplink content .        # pin it and set how much reaches the cloud
cabildo add --print .                 # show what would be written, change nothing
cabildo add --remove .                # undo both halves
```

Two things happen, and only these two:

1. **the folder is pinned** in `~/.cabildo/cloud.json` (absolute, symlinks
   resolved). Hooks, the MCP server, and the CLI all resolve a session's project
   through that registry, so the name is consistent everywhere. Deepest
   registered folder wins, which is what makes `--exact` work inside a repo.
2. **`.mcp.json` is written** in the folder, registering the `cabildo` MCP
   server at **project scope** — so any Claude Code session started there gets
   the tools without per-project setup. Other servers already in the file are
   left alone. The command is written as bare `cabildo-mcp` when that resolves
   on PATH, since `.mcp.json` is a checked-in file and your `~/.venv` path means
   nothing on a teammate's machine. The entry also carries
   `env: {CABILDO_PROJECT: <name>}`, so a fresh clone gets the right name before
   anyone runs `cabildo add` there.

The hooks stay user-scope (`scripts/install.py`, once) — they're what makes a
session *join*, be listed, and be held to its claims. `cabildo add` says so if
they're missing.

## Background services

The daemon (intents queue + desktop notifications) and the cloud mirror want to
outlive the terminal you started them in. `cabildo service` writes plain
**systemd --user** units to `~/.config/systemd/user/`:

```bash
cabildo service install --allow-push --allow-spawn   # the daemon
cabildo service install --sync                       # + mirror to the dashboard
cabildo service install --print                      # show the units, write nothing
cabildo service                                      # status
cabildo service logs -f                              # journalctl for both units
cabildo service restart --unit daemon
cabildo service uninstall
```

User-space, not a system unit: everything these processes touch is yours — the
bus under `~/.cabildo`, your worktrees, your `claude` credentials, your desktop
session. Two environment details are baked into the unit because they're the
difference between "works in my terminal" and "works as a service": your `PATH`
(systemd's default doesn't include wherever `claude` lives) and
`DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus` (without it `notify-send`
autolaunches a private bus and pops notifications nobody sees).

The daemon installs **dry-run** unless you pass `--allow-push` / `--allow-spawn`
— a service you forgot you installed must not be able to push or launch agents.
Services stop at logout unless you pass `--linger`.

Every unit cabildo writes is stamped, and the installer **refuses to overwrite a
unit file it didn't write** (it prints the existing `ExecStart` and stops).
`cabildo-sync.service` is a name people hand-roll — often around
`webapp/runner.py`, which does more than `cabildo sync` does. Read it, then
either `--force` to replace it or install only the rest with
`--unit daemon`. Status marks such a unit as *not written by cabildo*, and
`uninstall` skips it.

## Config

- `CABILDO_HOME` — where the DB lives (default `~/.cabildo`).
- `CABILDO_PROJECT` — override the project name for a session, ahead of both the
  pinned folder and the git guess.

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
