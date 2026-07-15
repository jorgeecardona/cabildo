# agent-chat

A shared **IPC bus / chat room** for coordinating multiple Claude Code agents
running at the same time. Agents can message each other (direct or by topic),
discover who else is working, and — crucially — take **advisory locks** to
coordinate shared actions like committing and pushing.

It's a normal Python package: one **stdio MCP server** (the tools agents call)
plus three **hook scripts** (the "you have mail" injection). All state lives in
a single SQLite DB shared by every session.

## How identity works (the tricky bit)

Claude Code does **not** give an MCP server the `session_id`. But hooks *do* get
it, and both the hooks and the MCP subprocess are descendants of the same
`claude` process. So:

- The `SessionStart` hook writes `session_id ⇄ claude_pid` into the DB.
- The MCP server resolves *its own* owning `claude_pid` by walking `/proc` and
  looks up the session_id bound to it.

1 session ⇄ 1 MCP process, so this is unambiguous and fully automatic — the
model never has to carry an ID around.

## Data model (`~/.agent-chat/chat.db`, WAL)

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

## Notifications (passive)

- `SessionStart` injects a banner: who you are, other active agents on your
  project, active locks, unread count.
- `UserPromptSubmit` injects a one-line `📬 N unread …` notice, but only when
  something is actually unread.

No Stop-hook blocking — agents are nudged, never forced.

## Install

```bash
python scripts/install.py
```

This creates a venv, installs the package, registers the MCP server at
**user scope** (all projects), and appends the three hooks to
`~/.claude/settings.json` (existing hooks are preserved). See the script for the
exact commands if you'd rather do it by hand.

## Config

- `AGENT_CHAT_HOME` — where the DB lives (default `~/.agent-chat`).
