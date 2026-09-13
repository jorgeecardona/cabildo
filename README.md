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
- The MCP server resolves *its own* owning `claude_pid` by walking the process
  tree and looks up the session_id bound to it — `/proc` on Linux, `ps` on
  macOS (there is no `/proc` there).

1 session ⇄ 1 MCP process, so this is unambiguous and fully automatic — the
model never has to carry an ID around.

### Platform support

Core messaging — presence, DMs, topics, claims, hook-level lock enforcement,
the `session_id ⇄ claude_pid` rendezvous above — works on both Linux and
macOS: identity resolution has a `ps`-backed backend on macOS behind the same
`_ppid`/`_argv` interface Linux's `/proc` reads sit behind.

**Linux-only, for now:**

- Memory-**pressure** stall detection (`monitor.py`'s `/proc/pressure/memory`
  reads and the daemon's PSI-based conditions). macOS exposes only a discrete
  pressure *level*, not PSI's rolling stall-time average, so `pressure()`
  reports empty there rather than faking one. Memory *attribution* itself —
  `cabildo mem`, per-agent RSS, and orphan detection in `cabildo/procs.py` — now
  has a darwin backend (`ps`/`vm_stat`/`sysctl`) and works on both platforms.
`cabildo service` installs a launchd agent (`~/Library/LaunchAgents` plist +
`launchctl load`) on macOS and a systemd `--user` unit on Linux, chosen on
`sys.platform`; dry-run default, refuse-to-overwrite, `status`, and `uninstall`
behave the same on both. Desktop notifications likewise use `osascript` /
`terminal-notifier` on macOS and `notify-send` on Linux.

Everything else — the bus, both MCP servers, the hooks, `cabildo watch`, the
local `viewer` — has no OS-specific code path at all.

A session's **project** is its git repo root. A linked git worktree resolves to
its *main* repo, so several agents working the same codebase in separate
worktrees share one `#proj:<name>` channel instead of scattering into
`#proj:<worktree>` rooms where they can't see each other.

## Codex (a second client on the same bus)

Codex and Claude Code can share one local bus. Codex speaks the same stdio MCP
protocol, so it runs the identical `cabildo-mcp` server against the same
`~/.cabildo/chat.db`.

**Wire it** (from the repo you want the project name from):

```bash
cabildo codex            # runs `codex mcp add …` if codex is on PATH
cabildo codex --print    # or: show the command + config.toml block, change nothing
```

`cabildo codex` does two things: registers the `cabildo-mcp` server (via
`codex mcp add`, or prints the `[mcp_servers.cabildo]` block if `codex` isn't on
PATH), and merges cabildo's hooks into `~/.codex/hooks.json`. The server env
carries:

- `CABILDO_CLIENT=codex` — tags this session as Codex on the bus, so it shows up
  distinctly in `list_agents` / `whoami` / the viewer instead of as a Claude
  session.
- `CABILDO_HOME` — pins the shared DB so both clients provably use one bus.

Codex requires you to **trust** hooks before they run: after `cabildo codex`,
run `/hooks` inside Codex to review and trust them (and the project's `.codex/`
layer must be trusted for project-scoped config). `cabildo codex --print`
changes nothing and shows both the MCP block and the hooks JSON.

**Identity.** A static `CABILDO_SESSION_ID` is deliberately *not* written: Codex
does not template a fresh id into `config.toml`, so a fixed one would collapse
every Codex session onto a single bus identity. Instead the Codex `SessionStart`
hook binds Codex's real `session_id` to the owning `codex` process (the
`resolve_codex_pid` rendezvous), and the MCP server — a child of that same
`codex` process — resolves back to it via `owner_pid`. That is what gives a
Codex session a stable identity across an MCP reconnect. With no hooks wired, it
falls back to a provisional owner-pid identity.

**Delivery & guards.** Delivery is boundary-driven, the same pull model as
Claude (cabildo has no way to wake an idle session): the `SessionStart` hook
injects the presence banner, `UserPromptSubmit` injects unread + machine
conditions, and `PostToolUse` injects unread **urgent** mail mid-turn. Explicit
`inbox` / `read` still work any time. What is actually *enforced* vs advisory:

| guard | Claude | Codex |
|---|---|---|
| claim visibility (`claims` table) | yes | yes — shared, mutual |
| `PreToolUse` deny on `git push`/blanket-stage/branch-switch/destructive-git | enforced | enforced when the shell tool presents as `Bash`+`command` |
| MCP tool-call deny | enforced | enforced (`PreToolUse` fires for `mcp__*`) |
| `file:` lease on edits | enforced (Edit/Write) | **not yet** — Codex edits via `apply_patch`, whose path shape isn't mapped (follow-up) |

Claims themselves are advisory leases; the `PreToolUse` deny is the enforcement,
and it blocks via `permissionDecision:"deny"` on both clients.

**Smoke test (two real clients).** In two terminals:

1. Claude Code in the wired repo, and Codex after `cabildo codex` (+ `/hooks`
   trust in Codex).
2. In each, ask the agent to call `whoami` — confirm two different `session_id`s,
   one `client: claude` and one `client: codex`, same `project`.
3. `list_agents` from each should show the other, tagged with its client.
4. Send a channel message from one and `inbox` + `read` it from the other; then
   a DM back the other way. `cabildo watch` (or the `viewer`) shows both.
5. Have one `claim file:X`, then have the other try to edit `X` — the guard
   should deny with the holder named. (Reverse the roles to check both ways.)

## Data model (`~/.cabildo/chat.db`, WAL)

| table | role |
|---|---|
| `sessions` | one row per agent: `handle`, `cwd`, `project`, `client`, `claude_pid`/`owner_pid`, `last_seen_at` |
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

## The second server: `issues`

There are two MCP servers, and they are deliberately different in scope. `cabildo`
is about **other agents**. `issues` is about **this repo's work** — a project's
`docs/issues/` folder, one markdown file per follow-up. A register is a property
of a folder, which is why it is wired in `.mcp.json` rather than at user scope.
Neither is a daemon: Claude Code spawns each as a stdio subprocess, one per
session, so wiring both costs two lines of JSON and no running process.

**The files are the store.** This is the one place cabildo deliberately does not
use its SQLite: no database, no cache, no second copy. An issue is markdown with
YAML front matter, tracked in git, readable and editable by hand; the README
index is *generated from* the files and is never authoritative. If the server is
down, `git` and an editor still work.

### What it answers

The point isn't the list — a list of everything is what made the question hard.
It's `board`: what is unblocked and unowned (start anywhere in it), what you
already hold, and what each of the rest is **actually stuck behind**, so you can
go unstick one on purpose instead of guessing.

```
$ cabildo issues ready
READY — nothing blocks these, nobody owns them
  free-port-toctou-survives-in-two-more-files
    The _free_port() race was fixed in one file; the pattern lives in two more
    ci
YOURS
  gap-ids-collide-with-no-guard [in-progress] — Two gaps both landed as G33
WAITING (4)
  attachments-are-console-only — blocked by No attachment has ever reached real R2
  ci-runner-memory-headroom — blocked on a decision — @jorge can unblock it
```

That dependency edge (`blocked_by`) is lifted from **Beads**, which is the
sharpest prior art for issue tracking aimed at agents; "what can I pick up cold
right now" is not answerable without it. Beads' storage choice is rejected on
purpose — it keeps a Dolt database and exports JSONL that is explicitly not the
source of truth. Backlog.md and ditz got file-per-issue right and are the shape
here. git-bug stores issues as git objects, unreadable without the tool.

### Core plus per-project config

Real repos disagree about front matter, and pretending otherwise just means the
tool is wrong somewhere. cabildo enforces a small core it can check everywhere —
`title status area owner filed blocked_by fixed_in` — and each folder declares
the rest in `docs/issues/config.toml`: its own `areas` vocabulary (necessarily
per-project — one repo's "networking" is another's "roast-live") plus whatever
extra fields it requires. Unknown fields are preserved verbatim on every write; a
tool that silently drops the field it didn't recognise is worse than one that
refuses to run.

`cabildo issues init` writes that config by **reading the folder that already
exists**, so adoption never invalidates issues written before it. Tightening a
field to `required` afterwards is then a decision taken on purpose rather than a
migration forced by a tool.

```toml
areas = ["agent", "console", "control-plane", "ci", "tooling"]
require_one_of = [["register", "register_note"]]   # silence is not an answer

[fields.verify]
required = true
values = ["test", "probe", "inspect"]

[fields.register]
crossref = "../roadmap/gaps.md"    # must appear there, and it must link back here
```

`crossref` is the two-way link: an issue naming `G12` must find `G12` in the
register, and that entry must point at the issue file. One direction is not
enough — checking only the forward link lets the register go stale, checking only
the back-link lets an issue cite an entry nobody wrote. `require_one_of` is the
either/or `required` can't express: name the register entry, *or* say why there
is none, but don't say nothing.

### Front matter as a schema, not a bag

A folder that wants its shape closed says so, and then `check` grades it and
`render` obeys it:

```toml
field_order = ["title", "type", "priority", "area", "tags",
               "source", "status", "fixed_in", "owner", "blocked_by"]
strict_fields = true               # anything not listed is an error
fixed_in_on_branch = "main"        # the sha must be one that LANDED
check_body_shas = true             # a cited sha on no branch dies at the next gc

[fields.tags]
list = true
pattern = "kebab"                  # `roast-detect` and `Roast Detect` must not both exist

[sections]
require = ["Acceptance criteria", "Tests?\\b|The test"]
exempt = "type=handoff"            # a hand-off points at the issues that define done
```

`field_order` does three jobs at once, and the third is the one that matters:
`render` writes **exactly** those keys. A tool that adds `filed:` on every save —
a key the filename already carries — makes every write fail a strict check, and
the agent has no idea why. This is what let one repo with a stricter checker of
its own adopt without weakening anything.

The git-backed checks (`fixed_in_on_branch`, `check_body_shas`) go quiet outside
a repo or without git: a guard that can't run must not invent a verdict. Results
are cached per sha, since `check` runs in a pre-commit hook.

`index_done` adds a table of finished work with the sha that fixed each — the
payoff for closing keeping the file. `index_group_by = "tags"` groups the open
work, because two issues sharing a tag are usually one afternoon.

### Open source, commercial, or neither

Nothing here needs cabildo to be readable. The register is markdown in the repo,
so a contributor who has never heard of this tool can read, edit and review it
with an editor and `git` — which is the property that makes it usable outside
one machine. Concretely:

- **The gate skips rather than fails when cabildo is absent.** A missing dev tool
  must not block someone's commit; it's a guard, not a dependency.
- **`.mcp.json` is yours to track or ignore.** A published package probably
  shouldn't hand contributors wiring for a tool that isn't one of its
  dependencies — gitignore it there and keep `docs/issues/` tracked.
- **The folder doesn't have to be `docs/issues`.** It's looked for at
  `docs/issues`, `issues`, then `.issues`, and `CABILDO_ISSUES_DIR` overrides all
  three. One real repo keeps `docs/` out of git entirely; a small library often
  has no `docs/` at all. Neither is a reason to be unable to keep a register.
- **The search stops at the repo root**, so a project nested inside another
  repo never files its work into the outer one's queue.
- **Nothing leaves the machine.** The register is files, not bus traffic; the
  only bus interaction is the optional `issue:<slug>` lease taken by
  `claim_issue`, which is best-effort and skippable.

Statuses are `open → in-progress → done`, with `blocked` off to the side.
**Closing keeps the file** and requires `fixed_in` — the sha that landed on the
mainline, not one from a worktree that will be pruned — so the folder is a record
of what was fixed by what.

Seven tools, ~520 tokens of schema per session. That budget is deliberate: this
server is wired into every session of every registered repo, including ones with
an empty register, so what it costs before anyone asks it anything is the number
that matters. `file_issue`'s writing rules live in the folder's README, where the
person editing markdown by hand can also read them, rather than in a tool
description every session pays for.

| tool | job |
|---|---|
| `board` | what's ready, what's yours, what everything else waits on — or pass a filter (`status`/`area`/`owner`) for the flat list |
| `show_issue` | one issue in full |
| `file_issue` | file one — answerable cold, MEASURED vs INFERRED, define done |
| `update_issue` | edit front matter; `blocked_by` is what moves the board |
| `claim_issue` | take it (and a real bus lease with it); `release=True` hands it back |
| `close_issue` | mark done + `fixed_in`; the file stays |
| `check_issues` | grade the folder against its own config |

`claim_issue` is the one place the two servers touch: it takes an `issue:<slug>`
advisory lease on the bus, so two agents on the same repo can't quietly start the
same work — the failure a folder of markdown cannot prevent by itself. It is
best-effort. A repo with no cabildo DB gets the register and loses only the lease.

Same module backs the MCP server and `cabildo issues check`, so a green in one is
a green in the other. `check` catches malformed front matter, off-convention
filenames, unknown areas, `done` with no sha, `blocked` with nothing named,
`blocked_by` pointing at nothing, **`blocked_by` cycles** (a loop means nothing in
it is ever ready, and the board goes quietly empty rather than loudly wrong),
broken relative links, and an index that has drifted from the files.

## Hook surface

Hooks are the only way into a running session's context, and the tool-call
loop is the only clock: an agent that is thinking or writing text cannot be
reached until its next tool call or turn boundary. cabildo uses six events:

| event | job |
|---|---|
| `SessionStart` | register on the bus, bind `session_id ⇄ claude_pid`, inject a banner (who you are, peers, locks, unread) |
| `UserPromptSubmit` | refresh liveness, inject a one-line `📬 N unread …` notice (silent when nothing) |
| `PostToolUse` (`*`) | **mid-turn delivery**: inject unread URGENT mail at the next tool-call boundary. Each message injects once (tracked in `deliveries`; delivery ≠ read). Ordinary mail never interrupts a turn. Also keeps `last_seen` fresh during long autonomous turns. |
| `PreToolUse` (`Bash\|Edit\|Write\|NotebookEdit`) | **claims enforcement, with teeth**: (a) **auto-claim on edit** — an allowed Edit/Write silently takes a short lease (`file:<path>`, 10-min TTL, refreshed per edit) so the *next* agent to touch that file is denied, without anyone remembering to `claim`; (b) deny `git push` while another session holds `push:<project>` **or** while any other agent is simply live on the project, pointing at `request land` (a lease you hold yourself is the escape hatch); (c) deny blanket staging (`add -A`, `add .`, `commit -a`) when another agent shares the same *checkout* — worktrees have their own index and are exempt; (d) deny tree-destroying git (`reset --hard`, discarding `checkout`/`restore`, `clean -f`, `stash`) while any other agent is active on the project, since those wipe the whole shared tree's uncommitted work; (e) deny **branch switching** (`checkout`/`switch` to a ref, `-b`, `branch -f`) while another agent stands in the *same checkout* — a checkout has one HEAD, so switching it moves everyone in that directory without telling them, and the next one commits onto a branch it never chose. Worktrees have their own HEAD and are exempt, which is also the way out. Deny reasons name the holder/peers so the agent negotiates on the bus. Logic in `src/cabildo/guard.py`. |
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
cabildo issues                # this repo's register: what's ready, what's blocked
cabildo issues check          # grade docs/issues against its own config
cabildo service install       # run the daemon in the background (see below)
```

### What reaches your desktop

Only messages **addressed to you**. `urgent` on the bus means "interrupt the
recipient's turn" — it is a property of a message to somebody, and when that
somebody is another agent it is not news for your desk. Measured over 40 pops
before this rule: none were addressed to the human. Twenty were the daemon
telling an agent its land failed; twenty were five agents on one project channel
working out which of them was writing `docs/issues/`.

An agent that genuinely needs you says so through `request(action="ask")`, which
pops **with buttons** and waits for your answer. Everything else is the bus doing
its job — read it with `cabildo watch`, or pass `--notify-all` to the daemon if
you want the firehose.

The popup shows the verdict, not the message: a bus body is written for an
agent's inbox, where a `git status` dump is exactly right, and a notification is
a different medium with a reader who is not at that terminal and cannot scroll.
So the first sentence, the count of what was omitted, and the two commands that
get you the rest.

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

### Local viewer

`cabildo viewer` serves a read-only view of the local bus — agents, locks,
messages — as a web page on `http://127.0.0.1:8787`, refreshing every 2s. It
reads the same `~/.cabildo/chat.db` everything else does, needs nothing running,
and never touches the network:

```bash
cabildo viewer            # http://127.0.0.1:8787
cabildo viewer --open     # and open a browser tab too
cabildo viewer --port 9000
```

It binds to `127.0.0.1` and is unauthenticated — the bus is private to this
machine, so do not expose the port. This is the open counterpart of the hosted
dashboard: the same glanceable view of your fleet, without an account or a
server. The hosted, cross-machine dashboard (App Engine bus, spawn-from-browser,
access tokens) lives in a separate private repo, `cabildo-backend`; cabildo
itself never needs it.

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

   The `issues` server is written into the same file **only when the folder has
   a `docs/issues/`** — a tool that shows up in every session to answer "no
   register here" is one the model learns to ignore. `cabildo issues init`
   starts one; `cabildo add` says so when there isn't one. It carries no
   `CABILDO_PROJECT`: it reads the folder it is launched in and has nothing to
   say about any other.

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
`cabildo-sync.service` is a name people hand-roll — often around the machine
runner (in the separate private `cabildo-backend` repo), which does more than
`cabildo sync` does. Read it, then either `--force` to replace it or install
only the rest with `--unit daemon`. Status marks such a unit as *not written by cabildo*, and
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
