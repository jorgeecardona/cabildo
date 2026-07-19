"""The backchat MCP server (stdio).

One instance runs per Claude Code session. It never receives the session_id from
Claude Code, so it resolves its identity via the shared-PID rendezvous: find the
owning `claude` process, then look up the session_id the SessionStart hook bound
to that PID. Everything else is a thin wrapper over backchat.store.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import store
from .actions import ACTIONS
from .context import git_project, resolve_claude_pid

mcp = FastMCP("backchat")

# Single source of truth: the daemon's registry decides what may be requested.
ACTION_NAMES = sorted(ACTIONS)


# --------------------------------------------------------------------------- #
# Identity resolution
# --------------------------------------------------------------------------- #
def _me(conn) -> str:
    """Resolve *this* session's id and refresh its liveness.

    Order matters. BACKCHAT_TAG is exact when present: we inherit it from the
    same `claude` the SessionStart hook ran under, so both ends agree without
    inferring anything. The PID rendezvous is the fallback for ordinary
    terminal sessions, and it is a guess — for a spawned `claude -p` it found a
    different claude entirely and split one agent across two bus identities.
    """
    tag = os.environ.get("BACKCHAT_TAG")
    if tag:
        sid = store.session_for_tag(conn, tag)
        if sid is not None:
            store.touch(conn, sid)
            return sid
    pid = resolve_claude_pid()
    sid = store.session_for_pid(conn, pid)
    if sid is None:
        # Provisional: register keyed by PID so we still work if the hook is
        # missing/misconfigured. The hook will reconcile the real id later.
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        root, name = git_project(cwd)
        sid = f"pid-{pid or os.getpid()}"
        store.register_session(
            conn, sid, cwd=cwd, project=root, project_name=name, claude_pid=pid
        )
    store.touch(conn, sid)
    return sid


def _handle(conn, sid: str) -> str | None:
    row = store.get_session(conn, sid)
    return row["handle"] if row else None


def _project(conn, sid: str) -> str | None:
    row = store.get_session(conn, sid)
    return row["project"] if row else None


# --------------------------------------------------------------------------- #
# Presence / identity tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def whoami() -> dict[str, Any]:
    """Who am I in the backchat bus: my session id, handle, project, the
    channels I'm subscribed to, and my unread count."""
    with store.connect() as conn:
        sid = _me(conn)
        row = store.get_session(conn, sid)
        return {
            "session_id": sid,
            "handle": row["handle"] if row else None,
            "project": row["project_name"] if row else None,
            "subscriptions": store.subscriptions_of(conn, sid),
            "unread": store.unread_summary(conn, sid)["count"],
        }


@mcp.tool()
def set_handle(handle: str) -> dict[str, Any]:
    """Set a friendly handle for this session (e.g. 'backend-refactor') so other
    agents can address and recognize you instead of a raw session id."""
    with store.connect() as conn:
        sid = _me(conn)
        store.set_handle(conn, sid, handle)
        return {"ok": True, "handle": handle, "session_id": sid}


@mcp.tool()
def list_agents(all_projects: bool = False) -> list[dict[str, Any]]:
    """List other active agents. By default only agents on the SAME project
    (git repo) as you; set all_projects=True to see everyone."""
    with store.connect() as conn:
        sid = _me(conn)
        project = None if all_projects else _project(conn, sid)
        rows = store.active_sessions(conn, project=project, exclude=sid)
        return [
            {
                "session_id": r["session_id"],
                "handle": r["handle"],
                "name": store.display_name(r),
                "project": r["project_name"],
                "cwd": r["cwd"],
            }
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Topics
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_topics() -> list[dict[str, Any]]:
    """List all chat channels/topics with subscriber and message counts."""
    with store.connect() as conn:
        _me(conn)
        return store.list_topics(conn)


@mcp.tool()
def join_topic(topic: str) -> dict[str, Any]:
    """Subscribe to a topic/channel so you receive its messages. Creates it if
    it doesn't exist. Use topics to organize coordination by idea/thread."""
    topic = topic.lstrip("#")
    with store.connect() as conn:
        sid = _me(conn)
        store.ensure_topic(conn, topic, created_by=sid)
        store.subscribe(conn, sid, topic)
        conn.commit()
        return {"ok": True, "topic": topic,
                "subscriptions": store.subscriptions_of(conn, sid)}


@mcp.tool()
def leave_topic(topic: str) -> dict[str, Any]:
    """Unsubscribe from a topic/channel."""
    topic = topic.lstrip("#")
    with store.connect() as conn:
        sid = _me(conn)
        store.unsubscribe(conn, sid, topic)
        conn.commit()
        return {"ok": True, "topic": topic,
                "subscriptions": store.subscriptions_of(conn, sid)}


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #
@mcp.tool()
def send(
    body: str,
    to: str | None = None,
    topic: str | None = None,
    urgent: bool = False,
) -> dict[str, Any]:
    """Send a message to the bus.

    Provide EITHER `to` (a session id or @handle for a direct message) OR
    `topic` (a channel name). If you give neither, it posts to your project's
    channel (#proj:<name>) so agents on the same repo see it. Set urgent=True to
    flag it. Messages are delivered as unread; recipients must call `read`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        me_handle = _handle(conn, sid)
        project = _project(conn, sid)

        to_session: str | None = None
        if to is not None:
            to_session = _resolve_recipient(conn, to)
            if to_session is None:
                return {"ok": False, "error": f"no active agent matches '{to}'"}
        elif topic is not None:
            topic = topic.lstrip("#")
            store.ensure_topic(conn, topic, created_by=sid)
        else:
            row = store.get_session(conn, sid)
            pname = row["project_name"] if row else None
            if not pname:
                return {"ok": False,
                        "error": "no project channel; specify `to` or `topic`"}
            topic = store.project_topic(pname)

        mid = store.post_message(
            conn,
            from_session=sid,
            from_handle=me_handle,
            body=body,
            topic=topic,
            to_session=to_session,
            project=project,
            urgent=urgent,
        )
        dest = f"@{to}" if to_session else f"#{topic}"
        return {"ok": True, "message_id": mid, "to": dest}


def _resolve_recipient(conn, to: str) -> str | None:
    """Resolve `to` to a session_id. Accepts an exact session id, a 'pid-...'
    id, a @handle / handle, or a short session-id prefix as shown by
    `list_agents` and message senders (most-recently-seen active match wins)."""
    to = to.lstrip("@")
    row = store.get_session(conn, to)
    if row is not None:
        return to
    match = conn.execute(
        "SELECT session_id FROM sessions WHERE handle=? AND status='active' "
        "ORDER BY last_seen_at DESC LIMIT 1",
        (to,),
    ).fetchone()
    if match:
        return match["session_id"]
    # display_name() advertises session_id[:8], so accept that back as an address.
    if len(to) >= 4:
        match = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ? AND status='active' "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (to.replace("%", "").replace("_", "") + "%",),
        ).fetchone()
        if match:
            return match["session_id"]
    return None


@mcp.tool()
def inbox(unread_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
    """Read messages addressed to you (direct messages + channels you follow).
    Defaults to unread only. This does NOT mark anything read — call `read` for
    that. Each item includes an `id` to pass to `read`."""
    with store.connect() as conn:
        sid = _me(conn)
        msgs = store.inbox(conn, sid, unread_only=unread_only, limit=limit)
        return [
            {
                "id": m["id"],
                "from": m["from_display"],
                "to": "you" if m["to_session"] else f"#{m['topic']}",
                "urgent": m["urgent"],
                "read": m["read"],
                "body": m["body"],
                "at": m["created_at"],
            }
            for m in msgs
        ]


@mcp.tool()
def read(
    message_ids: list[int] | None = None,
    topic: str | None = None,
    all: bool = False,
) -> dict[str, Any]:
    """Explicitly mark messages as read (they are never auto-read). Pass
    specific `message_ids`, or a `topic` to clear a channel, or all=True to
    clear your whole inbox. Returns how many were newly marked."""
    with store.connect() as conn:
        sid = _me(conn)
        n = store.mark_read(
            conn,
            sid,
            message_ids=message_ids,
            topic=topic.lstrip("#") if topic else None,
            all_unread=all,
        )
        remaining = store.unread_summary(conn, sid)["count"]
        return {"ok": True, "marked_read": n, "unread_remaining": remaining}


@mcp.tool()
def history(
    topic: str | None = None, with_agent: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Browse a conversation regardless of read state. Pass `topic` for a
    channel's history, or `with_agent` (session id / @handle) for a direct-
    message thread with that agent."""
    with store.connect() as conn:
        sid = _me(conn)
        between = None
        if with_agent is not None:
            other = _resolve_recipient(conn, with_agent)
            if other is None:
                return []
            between = (sid, other)
        msgs = store.history(
            conn,
            topic=topic.lstrip("#") if topic else None,
            between=between,
            limit=limit,
        )
        return [
            {
                "id": m["id"],
                "from": m["from_display"],
                "to": "DM" if m["to_session"] else f"#{m['topic']}",
                "body": m["body"],
                "at": m["created_at"],
            }
            for m in msgs
        ]


# --------------------------------------------------------------------------- #
# Coordination: advisory leases
# --------------------------------------------------------------------------- #
@mcp.tool()
def claim(resource: str, ttl_seconds: int = 600, note: str | None = None) -> dict[str, Any]:
    """Take an exclusive advisory lock on a named resource so other agents know
    to wait. Use for coordinating shared actions — e.g. resource='push:<repo>'
    before you commit & push, or 'file:src/app.py' while rewriting a file.

    Succeeds if the resource is free, expired, or already yours (refreshes it).
    Fails (ok=False) if another agent holds it, telling you who and for how long.
    The lock auto-expires after ttl_seconds so a crashed agent never deadlocks
    the bus. Call `release` when done. Locks are advisory — agents must honor
    them by convention.
    """
    with store.connect() as conn:
        sid = _me(conn)
        return store.acquire_claim(
            conn,
            resource=resource,
            holder_session=sid,
            holder_handle=_handle(conn, sid),
            project=_project(conn, sid),
            ttl_seconds=float(ttl_seconds),
            note=note,
        )


@mcp.tool()
def release(resource: str) -> dict[str, Any]:
    """Release an advisory lock you hold (see `claim`)."""
    with store.connect() as conn:
        sid = _me(conn)
        ok = store.release_claim(conn, resource=resource, holder_session=sid)
        return {"ok": ok, "resource": resource}


@mcp.tool()
def claims(all_projects: bool = False) -> list[dict[str, Any]]:
    """List active advisory locks. Defaults to your project; set
    all_projects=True for every lock on the bus. Check this before a shared
    action (like pushing) to see if another agent has claimed it."""
    with store.connect() as conn:
        sid = _me(conn)
        project = None if all_projects else _project(conn, sid)
        rows = store.list_claims(conn, project=project)
        return [
            {
                "resource": r["resource"],
                "held_by": r["holder"],
                "note": r["note"],
                "expires_in": r["expires_in"],
            }
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Control plane
# --------------------------------------------------------------------------- #
@mcp.tool()
def request(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask the daemon to do something deterministic on your behalf. Returns
    immediately with an intent id — this queues work, it does not run it.

    The daemon executes one intent at a time, so `land` cannot race another
    agent's `land`. That serialisation is the point: don't push yourself.

    Actions:
      land  — fetch, rebase onto the target, run the repo's gate, then push.
              params: {"cwd": "<your worktree>", "target": "main", "remote": "origin"}
              Requires a CLEAN tree: it lands your commits, it will not decide
              what to commit for you. On conflict it aborts and hands it back.
      spawn — launch a headless helper agent in its own git worktree.
              params: {"role": "pm"|"qa"|"ux", "task": "<real instruction>",
                       "repo": "<path>", "target": "main",
                       "model": "haiku"|"sonnet"|"opus" (optional, default omits)}
              You become the parent: the new agent is told who spawned it and
              will DM you its outcome. The daemon enforces a fleet cap, a
              spawn-depth limit, and the usage budget — a refusal is an answer,
              not something to retry. Prefer haiku/sonnet for grunt work; every
              agent burns the same shared usage window.
      ask   — put a question in front of the human as a desktop notification
              with buttons. params: {"question": "...", "options": ["yes","no"]}
              His answer arrives later as an urgent DM from @jorge — don't block.

    Poll `intents` for the outcome, or just wait — the daemon DMs you the result
    (urgently, if it failed).
    """
    with store.connect() as conn:
        sid = _me(conn)
        if action not in ACTION_NAMES:
            return {"ok": False, "error": f"unknown action {action!r}; allowed: {ACTION_NAMES}"}
        handle, project = _handle(conn, sid), _project(conn, sid)
        # Identity is stamped here, from the resolved session — never taken from
        # params. An agent must not be able to ask in someone else's name.
        p = {k: v for k, v in (params or {}).items() if not k.startswith("_")}
        p |= {"_from_session": sid, "_from_handle": handle, "_project": project}
        iid = store.request_intent(
            conn,
            from_session=sid,
            from_handle=handle,
            project=project,
            action=action,
            params=p,
        )
        return {"ok": True, "intent_id": iid, "status": "pending",
                "note": "queued — the daemon will DM you the result"}


@mcp.tool()
def intents(status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Look at the control queue: what's been asked of the daemon and how it
    went. Optionally filter by status (pending/running/done/failed/rejected)."""
    with store.connect() as conn:
        _me(conn)
        out = []
        for i in store.list_intents(conn, status=status, limit=limit):
            r = i.get("result") or {}
            out.append({
                "id": i["id"],
                "action": i["action"],
                "status": i["status"],
                "by": i["from_handle"] or i["from_session"][:8],
                "params": i["params"],
                "reason": r.get("reason"),
                "landed": r.get("landed"),
                "gate": r.get("gate"),
            })
        return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
