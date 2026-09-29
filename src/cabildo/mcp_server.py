"""The cabildo MCP server (stdio).

One instance runs per Claude Code session and is the single server a registered
project sees: the bus tools defined here plus the issues/agenda tools from
`cabildo.issues_mcp`, merged into one `cabildo` server by `_merge_issue_tools`.
The modules stay separate — bus coordination here, the register there — but a
session wires one server, and every tool shows up as `mcp__cabildo__*`.

It never receives the session_id from Claude Code, so it resolves its identity
via the shared-PID rendezvous: find the owning `claude` process, then look up the
session_id the SessionStart hook bound to that PID. Everything else is a thin
wrapper over cabildo.store.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config, names, store
from .actions import ACTIONS
from .context import git_project, resolve_claude_pid

mcp = FastMCP("cabildo")

# Single source of truth: the daemon's registry decides what may be requested.
ACTION_NAMES = sorted(ACTIONS)


# --------------------------------------------------------------------------- #
# Identity resolution
# --------------------------------------------------------------------------- #
def _client() -> str:
    """Which agent runtime owns this MCP server.

    Explicit via CABILDO_CLIENT — Codex (and any future non-Claude client) sets
    it before launching the server, since the PID rendezvous can only recognize
    a `claude` ancestor on its own. Absent the env, we are the Claude client,
    the only one whose ancestry resolve_claude_pid() can find unaided.
    """
    return (os.environ.get("CABILDO_CLIENT") or "claude").strip().lower() or "claude"


def _owner_pid(client: str) -> int:
    """The OS pid whose death should retire this session.

    For claude it is the `claude` ancestor the rendezvous already finds — the
    process the hooks and this server both descend from. For any other client
    the MCP server is launched as a direct child of the client, so the parent
    pid is the closest liveness anchor available without client-specific process
    discovery. Never the server's own pid: that dies with the connection, not
    the session, and would make every reconnect look like a crash.
    """
    if client == "claude":
        return resolve_claude_pid()
    ppid = os.getppid()
    return ppid if ppid > 1 else 0


def _me(conn) -> str:
    """Resolve *this* session's id and refresh its liveness.

    Order matters, strongest evidence first:

    1. CABILDO_TAG — exact for a spawned agent: inherited from the same launch
       the SessionStart hook ran under, so both ends agree without inference.
    2. CABILDO_SESSION_ID — an id the client states outright. This is the
       client-independent path and the answer to the ambiguous cases in #6
       (MCP reconnect, resumed session, several threads sharing one host
       process): a client that can set env before launching us gets an exact
       identity, Claude or not, without any ancestry guessing.
    3. The Claude PID rendezvous — bind to the id the hook recorded for the
       owning `claude`. Only meaningful for the claude client; a guess, but a
       good one for an ordinary terminal.
    4. Provisional owner-pid fallback — mint an id keyed by the owning process
       so we still function when the hook is missing (claude) or the client
       sent no explicit id. Scoped to the client so a reused pid never fuses two
       runtimes; a later hook run reconciles Claude's real id.
    """
    tag = os.environ.get("CABILDO_TAG")
    if tag:
        sid = store.session_for_tag(conn, tag)
        if sid is not None:
            store.touch(conn, sid)
            return sid

    client = _client()
    owner = _owner_pid(client)

    explicit = (os.environ.get("CABILDO_SESSION_ID") or "").strip()
    if explicit:
        if store.get_session(conn, explicit) is None:
            cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
            root, name = git_project(cwd)
            store.register_session(
                conn,
                explicit,
                cwd=cwd,
                project=root,
                project_name=name,
                client=client,
                owner_pid=owner,
                claude_pid=owner if client == "claude" else None,
            )
        store.touch(conn, explicit)
        return explicit

    if client == "claude":
        sid = store.session_for_pid(conn, owner)
        if sid is not None:
            store.touch(conn, sid)
            return sid
    else:
        sid = store.session_for_owner(conn, owner, client)
        if sid is not None:
            store.touch(conn, sid)
            return sid

    # Provisional. Keep claude's historic `pid-<pid>` id (existing rows,
    # _resolve_recipient's 'pid-...' shorthand); prefix other clients so their
    # provisional ids never collide with claude's.
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root, name = git_project(cwd)
    anchor = owner or os.getpid()
    sid = f"pid-{anchor}" if client == "claude" else f"{client}-pid-{anchor}"
    store.register_session(
        conn,
        sid,
        cwd=cwd,
        project=root,
        project_name=name,
        client=client,
        owner_pid=owner,
        claude_pid=owner if client == "claude" else None,
    )
    store.touch(conn, sid)
    return sid


def _handle(conn, sid: str) -> str | None:
    row = store.get_session(conn, sid)
    return row["handle"] if row else None


def _project(conn, sid: str) -> str | None:
    row = store.get_session(conn, sid)
    return row["project"] if row else None


def _project_name(conn, sid: str) -> str | None:
    row = store.get_session(conn, sid)
    return row["project_name"] if row else None


def _visible_projects(conn, sid: str) -> set[str] | None:
    """Dial A: the set of project *names* this session may see and talk to on
    the local bus — itself plus any peers its owner explicitly linked. Projects
    are isolated by default, so a name only appears here once linked
    (`cabildo project <yours> --link <other>`).

    Returns None when we can't determine our own project (no name, or the
    registry is unreadable). Callers treat None as 'no restriction' and fall
    back to pre-isolation behavior rather than hiding the whole bus from a
    session that has no project identity.
    """
    name = (_project_name(conn, sid) or "").strip().lower()
    if not name:
        return None
    try:
        from . import cloud

        return cloud.peers_visible(name)
    except Exception:
        return None


def _proj_topic_name(topic: str | None) -> str | None:
    """If `topic` is a per-project channel (proj:<name>), return <name> lowered,
    else None. The proj:<name> channel is a project's private space, so it's the
    unit Dial A gates on.
    """
    t = (topic or "").lstrip("#").strip()
    if t.startswith("proj:"):
        return t[len("proj:") :].strip().lower()
    return None


# --------------------------------------------------------------------------- #
# Presence / identity tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def whoami() -> dict[str, Any]:
    """Who am I in the cabildo bus: my session id, handle, project, the
    channels I'm subscribed to, and my unread count.
    """
    with store.connect() as conn:
        sid = _me(conn)
        row = store.get_session(conn, sid)
        return {
            "session_id": sid,
            "handle": row["handle"] if row else None,
            "client": (row["client"] if row else None) or "claude",
            "model": row["model"] if row else None,
            "role": row["role"] if row else None,
            "responsibilities": row["responsibilities"] if row else None,
            "state": store.presence_state(row),
            "project": row["project_name"] if row else None,
            "subscriptions": store.subscriptions_of(conn, sid),
            "unread": store.unread_summary(conn, sid)["count"],
        }


@mcp.tool()
def set_handle(handle: str) -> dict[str, Any]:
    """Set a friendly handle for this session (e.g. 'backend-refactor') so other
    agents can address and recognize you instead of a raw session id.
    """
    with store.connect() as conn:
        sid = _me(conn)
        store.set_handle(conn, sid, handle)
        return {"ok": True, "handle": handle, "session_id": sid}


@mcp.tool()
def set_role(role: str | None, responsibilities: str | None = None) -> dict[str, Any]:
    """Declare what this session is *for* on the bus, so peers can route work by
    function (via list_agents), not just by model. `role` is either one of the
    project's configured roster names (see the `[roles]` config table) or any
    freehand string. When `role` names a roster entry and you omit
    `responsibilities`, the roster's text is used; pass your own to override.
    Call set_role(None) to clear the role.
    """
    with store.connect() as conn:
        sid = _me(conn)
        row = store.get_session(conn, sid)
        cwd = row["cwd"] if row else None
        pname = row["project_name"] if row else None
        if role is None:
            store.set_role(conn, sid, None, None)
            return {"ok": True, "role": None, "session_id": sid}
        role = role.strip()
        roster = config.roles(cwd, pname)
        default = roster.get(role.lower())
        resolved = responsibilities if responsibilities is not None else default
        store.set_role(conn, sid, role, resolved)
        return {
            "ok": True,
            "role": role,
            "responsibilities": resolved,
            "from_roster": role.lower() in roster,
            "session_id": sid,
        }


@mcp.tool()
def record_decision(
    title: str,
    body: str | None = None,
    kind: str = "decision",
    refs: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    """Record a durable decision for this project, kept apart from chat so it
    survives context resets and dead sessions. Use it for what a peer coming in
    cold must know: a decision and why, a rejected alternative, a constraint, a
    warning. `refs` names the files/commit it touches; `tags` are freeform for
    filtering. `kind` is decision | fact | constraint | warning. Read them back
    with `read_decisions`.
    """
    kind = kind if kind in ("decision", "fact", "constraint", "warning") else "decision"
    with store.connect() as conn:
        sid = _me(conn)
        fid = store.add_decision(
            conn,
            title=title,
            body=body,
            kind=kind,
            refs=refs,
            tags=tags,
            session_id=sid,
            handle=_handle(conn, sid),
            project=_project(conn, sid),
            project_name=_project_name(conn, sid),
        )
        return {"ok": True, "id": fid, "kind": kind, "title": title}


@mcp.tool()
def read_decisions(
    query: str | None = None, kind: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """The durable decisions for this project, newest first: the coordination
    memory to read before touching unfamiliar work, instead of re-deriving it.
    `query` matches a substring in the title/body/refs/tags (e.g. read_decisions("auth")),
    `kind` narrows to decision|fact|constraint|warning. Recorded via `record_decision`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        return store.list_decisions(
            conn, project_name=_project_name(conn, sid), query=query, kind=kind, limit=limit
        )


@mcp.tool()
def list_agents(all_projects: bool = False) -> list[dict[str, Any]]:
    """List other active agents. By default only agents on the SAME project
    (git repo) as you; set all_projects=True to also see agents on projects
    LINKED to yours. Projects are isolated on the bus, so you never see a
    project you haven't linked (`cabildo project <yours> --link <other>`).
    """
    with store.connect() as conn:
        sid = _me(conn)
        visible = _visible_projects(conn, sid)
        project = None if all_projects else _project(conn, sid)
        rows = store.active_sessions(conn, project=project, exclude=sid)
        if visible is not None:
            rows = [r for r in rows if (r["project_name"] or "").strip().lower() in visible]
        return [
            {
                "session_id": r["session_id"],
                "handle": r["handle"],
                "name": store.display_name(r),
                # SELECT * over a migrated sessions table: the column is always present.
                "client": r["client"] or "claude",
                # Agent-card field: the peer's model, when known (filled by `record`
                # ingest for opted-in projects; NULL otherwise). Route by it, e.g.
                # ask a stronger model to review.
                "model": r["model"],
                # Agent-card capability label + what it owns: route work by
                # function, e.g. hand a review to whoever's role is 'reviewer'.
                "role": r["role"],
                "responsibilities": r["responsibilities"],
                # Derived presence: working (just acted) / idle (quiet) — richer
                # than online/offline, for deciding whom to hand work to.
                "state": store.presence_state(r),
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
    it doesn't exist. Use topics to organize coordination by idea/thread.
    """
    topic = topic.lstrip("#")
    with store.connect() as conn:
        sid = _me(conn)
        visible = _visible_projects(conn, sid)
        pj = _proj_topic_name(topic)
        if visible is not None and pj is not None and pj not in visible:
            return {
                "ok": False,
                "error": f"#{topic} is project '{pj}'s private channel, "
                f"not linked to yours — `cabildo project <yours> "
                f"--link {pj}` to allow",
            }
        store.ensure_topic(conn, topic, created_by=sid)
        store.subscribe(conn, sid, topic)
        conn.commit()
        return {"ok": True, "topic": topic, "subscriptions": store.subscriptions_of(conn, sid)}


@mcp.tool()
def leave_topic(topic: str) -> dict[str, Any]:
    """Unsubscribe from a topic/channel."""
    topic = topic.lstrip("#")
    with store.connect() as conn:
        sid = _me(conn)
        store.unsubscribe(conn, sid, topic)
        conn.commit()
        return {"ok": True, "topic": topic, "subscriptions": store.subscriptions_of(conn, sid)}


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
                return {"ok": False, "error": "no project channel; specify `to` or `topic`"}
            topic = store.project_topic(pname)

        # Dial A: projects are isolated. You may DM an agent, or post to a
        # project channel, only for your own project or one you've linked.
        visible = _visible_projects(conn, sid)
        if visible is not None:
            if to_session is not None:
                trow = store.get_session(conn, to_session)
                tname = ((trow["project_name"] if trow else "") or "").strip().lower()
                if tname and tname not in visible:
                    return {
                        "ok": False,
                        "error": f"'{to}' is on project '{tname}', not linked "
                        f"to yours — `cabildo project <yours> --link "
                        f"{tname}` to allow",
                    }
            else:
                pj = _proj_topic_name(topic)
                if pj is not None and pj not in visible:
                    return {
                        "ok": False,
                        "error": f"#{topic} is project '{pj}'s channel, not "
                        f"linked to yours — `cabildo project <yours> "
                        f"--link {pj}` to allow",
                    }

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


@mcp.tool()
def send_to_role(role: str, body: str, urgent: bool = False) -> dict[str, Any]:
    """Route a message by function: DM every active agent whose role is `role`,
    instead of naming a handle. This is how you hand work to whoever owns a
    domain (e.g. send_to_role("reviewer", "please review #123")) without knowing
    who is on shift. Delivers one DM per matching agent; yourself is excluded,
    and only agents on your project or a linked one are reached. Returns the
    recipients it hit — an empty list means no one currently holds that role.
    """
    with store.connect() as conn:
        sid = _me(conn)
        me_handle = _handle(conn, sid)
        project = _project(conn, sid)
        visible = _visible_projects(conn, sid)
        recipients: list[dict[str, Any]] = []
        for r in store.agents_with_role(conn, role, exclude=sid):
            tname = (r["project_name"] or "").strip().lower()
            if visible is not None and tname and tname not in visible:
                continue
            mid = store.post_message(
                conn,
                from_session=sid,
                from_handle=me_handle,
                body=body,
                topic=None,
                to_session=r["session_id"],
                project=project,
                urgent=urgent,
            )
            recipients.append(
                {"session_id": r["session_id"], "name": store.display_name(r), "message_id": mid}
            )
        return {"ok": True, "role": role, "delivered": len(recipients), "recipients": recipients}


def _resolve_recipient(conn, to: str) -> str | None:
    """Resolve `to` to a session_id. Accepts an exact session id, a 'pid-...'
    id, a @handle / handle, or a short session-id prefix as shown by
    `list_agents` and message senders (most-recently-seen active match wins).
    """
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
    # display_name() advertises a robot codename (BENDER-8F) — accept it back.
    by_name = store.session_for_robot_name(conn, to)
    if by_name:
        return by_name
    # ...and still accept the raw session_id[:8] prefix it replaced.
    if len(to) >= 4:
        match = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ? AND status='active' "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (to.replace("%", "").replace("_", "") + "%",),
        ).fetchone()
        if match:
            return match["session_id"]
    return None


# Long messages are the norm on this bus (status reports run ~1.8k chars), so an
# inbox of full bodies is what floods a session's context. Previews keep the
# inbox scannable; `get_message` fetches the one full body that matters.
#
# Per-message previews alone were not enough. Measured over two weeks, `inbox`
# was 39% of everything cabildo costs in tokens — and the average hid the
# problem: p50 1.7k chars, p90 4.8k, worst 43.6k in ONE call. A per-message cap
# cannot bound that, because the cost scales with how many messages are waiting;
# an agent that comes back to a busy channel pays the whole backlog at once. So
# the budget is on the WHOLE response, and the previews shrink to fit it.
PREVIEW_CHARS = 280
MIN_PREVIEW_CHARS = 90  # below this a preview stops being informative
INBOX_BUDGET_CHARS = 2000  # ~500 tokens for a full inbox call
DEFAULT_INBOX_LIMIT = 15
# id/from/to/at/urgent/truncated + JSON punctuation, per message. Budgeting only
# the bodies would blow the ceiling by ~60% on a crowded inbox, which is the
# number that made this worth fixing in the first place.
ENVELOPE_CHARS = 90


def _preview(body: str, limit: int = PREVIEW_CHARS) -> tuple[str, bool]:
    if len(body) <= limit:
        return body, False
    return body[:limit].rstrip() + " …", True


def _preview_budget(bodies: list[str], budget: int) -> int:
    """How many chars each preview gets: the budget split evenly, clamped.

    Even split rather than proportional — a 6k-char status report is not six
    times as urgent to skim as a 1k one, and letting one long message eat the
    budget is exactly the failure being fixed. Short messages don't use their
    whole share, so what they leave is handed back to the rest.
    """
    if not bodies:
        return PREVIEW_CHARS
    share = max(MIN_PREVIEW_CHARS, budget // len(bodies))
    slack = sum(share - len(b) for b in bodies if len(b) < share)
    long_ones = sum(1 for b in bodies if len(b) >= share)
    if long_ones and slack > 0:
        share += slack // long_ones
    return min(PREVIEW_CHARS, share)


@mcp.tool()
def inbox(
    unread_only: bool = True,
    limit: int = DEFAULT_INBOX_LIMIT,
    full: bool = False,
    budget_chars: int = INBOX_BUDGET_CHARS,
) -> dict[str, Any]:
    """Read messages addressed to you (direct messages + channels you follow).
    Defaults to unread only. This does NOT mark anything read — call `read` for
    that.

    Returns {"messages": [...], "more": N}. Each item has an `id`: pass it to
    `read` to clear it, or to `get_message` for the full body. Bodies are
    previewed, and the whole response is kept under `budget_chars` (~500 tokens)
    so a busy channel can't flood your context — previews shrink when many
    messages are waiting. `more` counts what didn't fit; call again after
    reading. full=True returns complete bodies and ignores the budget: use it
    when you already know the inbox is small.
    """
    with store.connect() as conn:
        sid = _me(conn)
        limit = max(1, limit)
        msgs = store.inbox(conn, sid, unread_only=unread_only, limit=limit)
        # The real remainder, not "at least one more": an agent deciding whether
        # to drain its inbox needs the size of what it hasn't seen.
        more = max(0, store.inbox_count(conn, sid, unread_only=unread_only) - len(msgs))

        usable = max(0, budget_chars - ENVELOPE_CHARS * len(msgs))
        share = PREVIEW_CHARS if full else _preview_budget([m["body"] or "" for m in msgs], usable)
        out: list[dict[str, Any]] = []
        for m in msgs:
            item = {
                "id": m["id"],
                "from": m["from_display"],
                "to": "you" if m["to_session"] else f"#{m['topic']}",
                "at": m["created_at"],
            }
            if m["urgent"]:
                item["urgent"] = True  # false is the norm; don't spend on it
            if not unread_only and m["read"]:
                item["read"] = True
            if full:
                item["body"] = m["body"]
            else:
                preview, truncated = _preview(m["body"] or "", share)
                item["body"] = preview
                if truncated:
                    item["truncated"] = True  # get_message(id) for the rest
            out.append(item)
        return {"messages": out, "more": more}


@mcp.tool()
def get_message(message_id: int) -> dict[str, Any] | None:
    """Fetch one message's full body by id (from an `inbox` preview). Returns
    null if that message is not in your inbox. Does NOT mark it read.
    """
    with store.connect() as conn:
        sid = _me(conn)
        m = store.get_message(conn, sid, message_id)
        if m is None:
            return None
        return {
            "id": m["id"],
            "from": m["from_display"],
            "to": "you" if m["to_session"] else f"#{m['topic']}",
            "urgent": m["urgent"],
            "read": m["read"],
            "body": m["body"],
            "at": m["created_at"],
        }


@mcp.tool()
def read(
    message_ids: list[int] | None = None,
    topic: str | None = None,
    all: bool = False,
) -> dict[str, Any]:
    """Explicitly mark messages as read (they are never auto-read). Pass
    specific `message_ids`, or a `topic` to clear a channel, or all=True to
    clear your whole inbox. Returns how many were newly marked.
    """
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
    message thread with that agent.
    """
    with store.connect() as conn:
        sid = _me(conn)
        visible = _visible_projects(conn, sid)
        pj = _proj_topic_name(topic)
        if visible is not None and pj is not None and pj not in visible:
            return []
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
    action (like pushing) to see if another agent has claimed it.
    """
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


@mcp.tool()
def conditions() -> dict[str, Any]:
    """What is currently true about this machine: memory pressure, processes
    left behind by agents that ended, anything the human raised by hand.

    You are told about these automatically when they change, so call this only
    when you want the current state on purpose — before starting a build, a test
    suite, or a batch of parallel work, or after being told memory is tight and
    wanting to know whether it still is. `mine` is your own share: if it is
    large, the memory the alert is about is yours to release.

    Read-only. Conditions are raised by the daemon, which measures, and by the
    human, who knows things it cannot — not by agents. Anything you want to tell
    the fleet yourself is a `send`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        out = []
        for c in store.active_conditions(conn):
            mine = (c["data"].get("by_session") or {}).get(sid)
            out.append(
                {
                    "key": c["key"],
                    "level": c["level"],
                    "headline": c["headline"],
                    "advice": c["advice"],
                    "mine": mine,
                    "raised_ago": round(c["age"]),
                    "source": c["source"],
                    "details": {k: v for k, v in c["data"].items() if k != "by_session"},
                }
            )
        return {
            "host": store.hostname(),
            "conditions": out,
            "note": "nothing raised — the machine is fine" if not out else None,
        }


# --------------------------------------------------------------------------- #
# Control plane
# --------------------------------------------------------------------------- #
@mcp.tool()
def request(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask the daemon to do something deterministic on your behalf. Returns
    immediately with an intent id — this queues work, it does not run it.

    The daemon executes one intent at a time, so `push` cannot race another
    agent's `push`. That serialisation is the point where cabildo manages pushes;
    on a repo set to [push].mode = "manual" it does not, and you push by hand.

    Actions:
      push  — fetch, rebase onto the target, run the repo's gate, then push your
              committed work to origin/<target>. It moves work to the REMOTE; it
              does not change your local <target> (cabildo catches that up for you
              after a successful push). params: {"cwd": "<your worktree>",
              "target": "main", "remote": "origin"}. Requires a CLEAN tree: it
              pushes your commits, it will not decide what to commit for you. On
              conflict it aborts and hands it back. Gated by [push].mode: a repo
              set to "manual" pushes by hand, and a push there is refused.
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
        return {
            "ok": True,
            "intent_id": iid,
            "status": "pending",
            "note": "queued — the daemon will DM you the result",
        }


@mcp.tool()
def intents(status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Look at the control queue: what's been asked of the daemon and how it
    went. Optionally filter by status (pending/running/done/failed/rejected).
    """
    with store.connect() as conn:
        _me(conn)
        out = []
        for i in store.list_intents(conn, status=status, limit=limit):
            r = i.get("result") or {}
            out.append(
                {
                    "id": i["id"],
                    "action": i["action"],
                    "status": i["status"],
                    "by": i["from_handle"] or names.robot_name(i["from_session"]),
                    "params": i["params"],
                    "reason": r.get("reason"),
                    "pushed": r.get("pushed"),
                    "gate": r.get("gate"),
                }
            )
        return out


@mcp.tool()
def branches(repo: str | None = None, target: str = "main") -> dict[str, Any]:
    """Inventory this repo's worktrees and branches — the zoo coding agents leave
    behind — and who each one belongs to. Read-only: it runs only read-only git
    and never fetches, so ahead/behind is against the LOCAL origin/<target>.

    Each worktree and each bare branch (one with no worktree) comes back with its
    branch, HEAD, ahead/behind the trunk, whether it is already in the trunk,
    dirty count (tracked changes only), and the cabildo agent living there when
    one is known (live session, else the spawn ledger). `repo` defaults to your
    own working directory.
    """
    from . import branches as _branches

    with store.connect() as conn:
        sid = _me(conn)
        row = store.get_session(conn, sid)
        cwd = repo or (row["cwd"] if row else None)
    return _branches.inventory(cwd=cwd, target=target)


@mcp.tool(structured_output=False)
def standup(hours: float = 24.0) -> str:
    """Who touched what over the last `hours`: messages, tasks in flight (claim = flow),
    spawns, active agents, and token cost — from cabildo's own record, not scraped chat.
    """
    from . import standup as _standup

    with store.connect() as conn:
        return _standup.render(_standup.collect(conn, hours=hours))


def _merge_issue_tools() -> None:
    """Register the issues/agenda tools onto this one `cabildo` server.

    The issues module keeps its own module-level tool definitions (so it stays a
    separate concern and works with no bus DB); here we adopt them into the single
    server every session sees. Idempotent — re-adopting the same tool is a no-op.
    A name shared by two DIFFERENT tools is a real conflict and raises, so the
    build fails rather than silently dropping one.
    """
    from . import issues_mcp

    ours = mcp._tool_manager._tools
    for name, tool in issues_mcp.mcp._tool_manager._tools.items():
        if name in ours and ours[name] is not tool:
            raise RuntimeError(f"MCP tool name collision between bus and issues: {name!r}")
        ours[name] = tool


_merge_issue_tools()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
