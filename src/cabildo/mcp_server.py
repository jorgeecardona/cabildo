"""The cabildo MCP server (stdio).

One instance runs per Claude Code session and is the single server a registered
project sees: every tool shows up as `mcp__cabildo__*`. Issue tracking is not
cabildo's job — a project that wants a register wires that server itself.

It never receives the session_id from Claude Code, so it resolves its identity
via the shared-PID rendezvous: find the owning `claude` process, then look up the
session_id the SessionStart hook bound to that PID. Everything else is a thin
wrapper over cabildo.store.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

from . import cloud, config, names, questions, store
from .actions import ACTIONS
from .context import git_project, resolve_claude_pid
from .mcp_stdio import McpServer

mcp = McpServer("cabildo")

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
    are isolated by default, so a name only appears here once the human links them.

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


def _proj_topic_name(conn, topic: str | None) -> str | None:
    """The project key of a ``project`` channel, else None.

    The channel's ``scope`` column decides; a name with no topics row falls back to
    ``store.implied_scope``. A project channel is that project's private space, so
    it is the unit Dial A gates on.
    """
    t = (topic or "").lstrip("#").strip()
    if not t:
        return None
    ch = store.channel(conn, t)
    scope, project = (ch.scope, ch.project) if ch else store.implied_scope(t)
    if scope != "project":
        return None
    return (project or "").strip().lower() or None


def _may_reach_project(
    conn,
    my_project: str | None,
    target_project: str | None,
    capability: str,
    session: str | None = None,
) -> bool:
    """Whether a session in `my_project` may reach `target_project` with
    `capability` ('listen'|'send'). True for your own project, a legacy symmetric
    `--link` peer (cloud.json), or a granted directional row (issue #26). A
    session with no project identity (human/daemon) is unrestricted.
    """
    mine = (my_project or "").strip().lower()
    tgt = (target_project or "").strip().lower()
    if not mine or not tgt or mine == tgt:
        return True
    try:
        from . import cloud

        if tgt in cloud.peers_visible(mine):
            return True
    except Exception:
        pass
    return store.has_grant(conn, mine, "project", tgt, capability, session)


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


@mcp.tool(name="help")
async def help_tool(topic: str | None = None, query: str | None = None) -> dict[str, Any]:
    """How to use cabildo well. With no argument: the start guide and the list of topics.

    ``topic`` returns one page (``start``, ``tools``, ``gateway``, ``channels``, ``roles``,
    ``browser``, ``crons``). ``tools`` lists every cabildo tool with its first line. ``query``
    searches every page for the lines that hold all its words.
    """
    from . import help as guide

    root = guide.docs_dir()
    if root is None:
        return {"ok": False, "error": "the guide pages are not installed with this build"}
    names = [
        {"name": t.name, "title": t.title, "summary": t.summary} for t in guide.topics(root)
    ] + [{"name": "tools", "title": "Tools", "summary": "Every cabildo tool, one line each."}]
    if query:
        hits = guide.search(root, query)
        return {"ok": True, "query": query, "hits": [h.__dict__ for h in hits], "topics": names}
    if topic is None:
        return {
            "ok": True,
            "topic": guide.START,
            "text": guide.read(root, guide.START),
            "topics": names,
        }
    if topic.strip().lower() == "tools":
        lines = [
            f"- {t.name}: {(t.description or '').strip().splitlines()[0] if t.description else ''}"
            for t in sorted(await mcp.list_tools(), key=lambda t: t.name)
        ]
        return {"ok": True, "topic": "tools", "text": "\n".join(lines)}
    text = guide.read(root, topic)
    if text is None:
        return {"ok": False, "error": f"no topic {topic!r}", "topics": names}
    return {"ok": True, "topic": topic.strip().lower(), "text": text}


@mcp.tool()
def version() -> dict[str, Any]:
    """The cabildo version this MCP server is running, and the daemon's if one is
    up. A server keeps the code it started with, so after an upgrade the two can
    differ from what is installed: compare them, then restart the older one.
    """
    import json
    import sys

    from . import __version__
    from .daemon import info_path

    daemon = None
    try:
        info = json.loads(info_path().read_text())
        daemon = {"version": info["version"], "pid": info["pid"], "exe": info["exe"]}
    except (OSError, ValueError, KeyError):
        pass
    return {"version": __version__, "exe": sys.executable, "daemon": daemon}


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


_MINUTES_PREVIEW = 200


@mcp.tool()
def record_minutes(
    title: str,
    body: str,
    session: str | None = None,
    project_level: bool = False,
    decision_ids: list[int] | None = None,
    tags: str | None = None,
    revises: str | None = None,
) -> dict[str, Any]:
    """Leave the minutes of a session: the written record of what it worked on, what was
    decided and why, and what is still open. `body` is markdown. They stay after the session
    ends and any session of this project can read them with `read_minutes`.

    By default the minutes are about your own session; pass `session` to write them for
    another one, or `project_level=True` for minutes that belong to no single session.
    `decision_ids` cites decisions from `record_decision`. Minutes are never edited: pass
    `revises` (the guid of earlier minutes) to leave a new revision, and the first one stays.
    """
    with store.connect() as conn:
        sid = _me(conn)
        author = "human" if sid == store.HUMAN_SESSION else _handle(conn, sid) or sid
        try:
            row = store.add_minutes(
                conn,
                title=title,
                body=body,
                author=author,
                session_id=None if project_level else (session or sid),
                project=_project(conn, sid),
                project_name=_project_name(conn, sid),
                decision_ids=",".join(str(i) for i in decision_ids) if decision_ids else None,
                tags=tags,
                revises=revises,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "id": row["id"], "guid": row["guid"], "rev": row["rev"]}


@mcp.tool()
def read_minutes(
    ref: str | None = None,
    rev: int | None = None,
    session: str | None = None,
    since_hours: float | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Read session minutes left with `record_minutes`.

    With `ref` (an id or a guid) it returns that one record in full, newest revision unless
    `rev` is set, and lists every revision number. Without it, the newest minutes of this
    project, newest first, with the body cut to a short preview; narrow by `session` or
    `since_hours`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        if ref:
            one = store.get_minutes(conn, ref, rev)
            return [one] if one else []
        since = time.time() - since_hours * 3600 if since_hours else None
        rows = store.list_minutes(
            conn,
            project_name=_project_name(conn, sid),
            session_id=session,
            since=since,
            limit=limit,
        )
        for r in rows:
            text = str(r["body"])
            r["body"] = text if len(text) <= _MINUTES_PREVIEW else text[:_MINUTES_PREVIEW] + "…"
        return rows


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
def note_add(text: str, type: str = "topic") -> dict[str, Any]:
    """Capture a loose end for THIS session (issue #34): a topic you just opened,
    a follow-up you will not do now, something half-finished. Add one the moment a
    new thread starts, so it is not lost when the conversation moves on — `/wrap-up`
    reads these back instead of re-reading the whole conversation. `type` is
    topic | need | bug | idea | unfinished. The note starts `open`; close it with
    `note_done` when you finish it here, or `note_park` when you promote it to a
    project issue. List them with `notes`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        nid = store.add_note(
            conn,
            text=text,
            type=type,
            session_id=sid,
            handle=_handle(conn, sid),
            project=_project(conn, sid),
            project_name=_project_name(conn, sid),
        )
        return {"ok": True, "id": nid, "type": type, "text": text}


@mcp.tool()
def notes(
    state: str = "open", all_sessions: bool = False, type: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """The session notes for THIS session, oldest first — the loose ends to walk
    at `/wrap-up`. `state` filters open | done | parked (pass an empty string for
    all); `all_sessions=True` widens to every session on this project (to pick up
    another session's open threads); `type` narrows to one of
    topic|need|bug|idea|unfinished. Added with `note_add`, triaged with
    `note_done` / `note_park`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        return store.list_notes(
            conn,
            session_id=None if all_sessions else sid,
            project_name=_project_name(conn, sid) if all_sessions else None,
            state=state or None,
            type=type,
            limit=limit,
        )


@mcp.tool()
def note_done(id: int) -> dict[str, Any]:
    """Mark a session note finished (state -> done): you completed it in this
    session. See `notes` for the ids.
    """
    with store.connect() as conn:
        _me(conn)
        row = store.set_note_state(conn, id, state="done")
        return {"ok": bool(row), "id": id, "note": row}


@mcp.tool()
def note_park(id: int, ref: str | None = None) -> dict[str, Any]:
    """Park a session note (state -> parked): it leaves the session to live on as
    a project issue so a later session picks it up without copy-paste. File that
    issue first, in whatever tracker this project uses, and pass its name as
    `ref` so the note records where it went. See `notes` for the ids.
    """
    with store.connect() as conn:
        _me(conn)
        row = store.set_note_state(conn, id, state="parked", parked_ref=ref)
        return {"ok": bool(row), "id": id, "note": row}


def _node(row: Any) -> str | None:
    """The short node id of a paired machine's agent, or None for a local one."""
    client = str(row["client"] or "")
    return (
        client[len(store.REMOTE_PREFIX) :][:8] if client.startswith(store.REMOTE_PREFIX) else None
    )


@mcp.tool()
def list_agents(all_projects: bool = False) -> list[dict[str, Any]]:
    """List other active agents. By default only agents on the SAME project
    (git repo) as you; set all_projects=True to also see agents on projects
    LINKED to yours. Projects are isolated on the bus, so you never see a
    project that is not linked. To reach one, use `request_access`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        visible = _visible_projects(conn, sid)
        project = None if all_projects else _project(conn, sid)
        rows = store.active_sessions(conn, project=project, exclude=sid)
        if visible is not None:
            rows = [r for r in rows if (r["project_name"] or "").strip().lower() in visible]
        # A paired machine's agents are always listed: the pairing is the consent,
        # and their project need not exist here (a project is a local notion).
        have = {r["session_id"] for r in rows}
        rows += [
            r
            for r in store.active_sessions(conn, exclude=sid)
            if _node(r) and r["session_id"] not in have
        ]
        return [
            {
                "session_id": r["session_id"],
                "handle": r["handle"],
                "name": store.display_name(r) + (f" ({_node(r)})" if _node(r) else ""),
                # SELECT * over a migrated sessions table: the column is always present.
                "client": r["client"] or "claude",
                # The paired machine this agent runs on (short node id); None if local.
                "node": _node(r),
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
def list_topics(
    prefix: str | None = None,
    project: str | None = None,
    include_unused: bool = False,
    min_messages: int = 0,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List chat channels/topics with subscriber and message counts, newest activity first.

    By default only topics that have a message or that you subscribe to, 50 at a time: most
    `proj:` channels are created and never used. Set `include_unused=True` for all of them.
    `prefix` (for example `proj:` or `ws:`), `project` and `min_messages` narrow the list. Page
    with `offset` until fewer than `limit` come back.
    """
    with store.connect() as conn:
        sid = _me(conn)
        return store.list_topics(
            conn,
            session=sid,
            prefix=prefix,
            project=project,
            include_unused=include_unused,
            min_messages=min_messages,
            recent_first=True,
            limit=limit,
            offset=offset,
        )


@mcp.tool()
def join_topic(topic: str, scope: str = "local") -> dict[str, Any]:
    """Subscribe to a channel so you receive its messages. Creates it if it doesn't
    exist. Use channels to organize coordination by idea/thread.

    ``scope`` applies only when this call creates the channel: ``local`` (default)
    keeps its posts on this machine, ``shared`` sends them to every paired machine.
    A ``project`` channel comes from the session's project, never from this call.
    """
    topic = topic.lstrip("#")
    if scope not in ("local", "shared"):
        return {"ok": False, "error": "scope must be 'local' or 'shared'"}
    with store.connect() as conn:
        sid = _me(conn)
        my_project = _project_name(conn, sid)
        pj = _proj_topic_name(conn, topic)
        if pj is not None and not _may_reach_project(conn, my_project, pj, "listen", sid):
            return {
                "ok": False,
                "error": f"#{topic} is project '{pj}'s private channel, not reachable "
                f"from yours ('{(my_project or '').strip().lower()}') — use `request_access` "
                "to ask for a grant",
            }
        # A gated (private) topic needs a 'listen' grant to join (issue #26).
        if pj is None and store.topic_is_gated(conn, topic) and (my_project or "").strip():
            mine = (my_project or "").strip().lower()
            if not store.has_grant(conn, mine, "topic", topic, "listen"):
                return {
                    "ok": False,
                    "error": f"#{topic} is a gated topic — `request_access` for a "
                    f"'listen' grant to join",
                }
        if pj is not None:
            store.ensure_topic(conn, topic, project=pj, created_by=sid, scope="project")
        else:
            store.ensure_topic(
                conn, topic, created_by=sid, scope="shared" if scope == "shared" else "local"
            )
        store.subscribe(conn, sid, topic)
        conn.commit()
        ch = store.channel(conn, topic)
        result: dict[str, Any] = {
            "ok": True,
            "topic": topic,
            "scope": ch.scope if ch else scope,
            "subscriptions": store.subscriptions_of(conn, sid),
        }
        if result["scope"] == "shared":
            result.update(_shared_reach())
        return result


def _shared_reach() -> dict[str, Any]:
    """How many paired machines a shared channel reaches right now (#48): the count, and a
    `reason` naming the way to pair when none answers. Never raises; a failure reads as no data.
    """
    try:
        rows = peers()
    except Exception:
        return {}
    down = [r for r in rows if not r["last_pull"] or r["last_pull"].get("error")]
    reach: dict[str, Any] = {"peers": {"paired": len(rows), "reachable": len(rows) - len(down)}}
    if len(rows) - len(down) == 0:
        why = "no machine is paired" if not rows else "no paired machine answered the last sync"
        reach["reason"] = (
            f"{why}, so posts here stay on this machine. "
            'Pair one with request(action="pair"), then {"pin": "..."} on the other machine.'
        )
        reach["unreachable"] = [
            {"address": r["address"], "error": (r["last_pull"] or {}).get("error")} for r in down
        ]
    return reach


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
# Directory (discovery) + grants (cross-project consent) — issue #26
# --------------------------------------------------------------------------- #
def _may_decide_grant(conn, sid: str, grant: dict[str, Any]) -> bool:
    """Who may grant/deny/revoke a request. The human always may. A `project`
    request is decided by an agent in the asked-for project when that project's
    `[grants].approver` is "project". A `topic` request is decided by an already
    admitted member (a project holding a granted listen on it). An `agent`
    request is decided by that agent itself.
    """
    if sid == store.HUMAN_SESSION:
        return True
    kind, resource = grant["resource_kind"], grant["resource"]
    my = (_project_name(conn, sid) or "").strip().lower()
    if kind == "project":
        return bool(my) and my == resource and config.grants_approver(None, resource) == "project"
    if kind == "topic":
        return store.has_grant(conn, my, "topic", resource, "listen") if my else False
    if kind == "agent":
        return sid == resource
    return False


@mcp.tool()
def directory() -> list[dict[str, Any]]:
    """The cross-project phone book: every project active on the bus with its
    active-agent count and the roles present. Discovery only — no message content
    and no channel contents, so seeing a project here does NOT mean you receive
    its chatter. Find a project you are not linked to, then `request_access` to
    reach it.
    """
    with store.connect() as conn:
        _me(conn)
        return store.directory(conn)


@mcp.tool()
def request_access(
    resource: str,
    capability: str = "send",
    kind: str = "project",
    note: str | None = None,
) -> dict[str, Any]:
    """Ask another project (or a gated topic) for consent to reach it — the
    deliberate alternative to accidental cross-project contact (issue #26).

    `kind` is "project" or "topic"; `capability` is "send" (post/DM) or "listen"
    (subscribe/read). This files a directional request from YOUR project; the
    target decides. Routing follows the target's `[grants].approver`: "human"
    (default) notifies the human; "project" lets an agent there decide with
    `grant`/`deny`. The knock is announced to the target so someone can act on
    it. Poll `grants` for the outcome, or wait for the DM.
    """
    kind = kind.strip().lower()
    capability = capability.strip().lower()
    if kind not in ("project", "topic"):
        return {"ok": False, "error": "kind must be 'project' or 'topic'"}
    if capability not in store.GRANT_CAPS:
        return {"ok": False, "error": f"capability must be one of {store.GRANT_CAPS}"}
    resource = resource.lstrip("#").strip()
    if kind == "project":
        resource = store._norm_grant_resource(kind, resource)  # a path names its folder (#52)
    with store.connect() as conn:
        sid = _me(conn)
        me_handle = _handle(conn, sid)
        my_project = (_project_name(conn, sid) or "").strip().lower()
        if not my_project:
            return {"ok": False, "error": "you have no project identity to request from"}
        target = resource.lower()
        if kind == "project" and target == my_project:
            return {"ok": False, "error": "that is your own project"}
        gid = store.upsert_grant(
            conn,
            grantee=my_project,
            resource_kind=kind,
            resource=resource,
            capability=capability,
            status="requested",
            requested_by=sid,
            note=note,
        )
        who = store.display_name(store.get_session(conn, sid), sid)
        knock = (
            f"🔔 access request #{gid}: project '{my_project}' asks to {capability} "
            f"{'your project' if kind == 'project' else f'#{resource}'}"
            f"{' — ' + note if note else ''}. An approver here can `grant({gid})` or `deny({gid})`."
        )
        # Announce the knock: to the target project's channel when it's a project
        # request (its agents are subscribed there), and always to the human.
        if kind == "project":
            store.post_message(
                conn,
                from_session=sid,
                from_handle=me_handle,
                body=knock,
                topic=store.project_topic(target),
                project=my_project,
                urgent=False,
            )
        store.post_message(
            conn,
            from_session=sid,
            from_handle=me_handle,
            body=knock,
            to_session=store.HUMAN_SESSION,
            project=my_project,
            urgent=True,
        )
        conn.commit()
        return {"ok": True, "grant_id": gid, "status": "requested", "from": who}


@mcp.tool()
def grant(request_id: int) -> dict[str, Any]:
    """Approve a pending access request (see `grants`). Only an approver for the
    target may: the human, or — per the target's `[grants].approver` policy — an
    agent in the asked-for project (for a project request) or an admitted member
    (for a topic). The requester is DM'd the outcome.
    """
    return _decide(request_id, "granted")


@mcp.tool()
def deny(request_id: int) -> dict[str, Any]:
    """Refuse a pending access request (see `grants`). Same authority rules as
    `grant`. The requester is DM'd the outcome.
    """
    return _decide(request_id, "denied")


@mcp.tool()
def revoke(grant_id: int) -> dict[str, Any]:
    """Withdraw a previously granted access (see `grants`). Same authority rules
    as `grant`; takes effect immediately on the next send/join.
    """
    return _decide(grant_id, "revoked")


def _decide(grant_id: int, status: str) -> dict[str, Any]:
    with store.connect() as conn:
        sid = _me(conn)
        me_handle = _handle(conn, sid)
        g = store.get_grant(conn, grant_id)
        if g is None:
            return {"ok": False, "error": f"no grant #{grant_id}"}
        if not _may_decide_grant(conn, sid, g):
            return {"ok": False, "error": f"you are not an approver for grant #{grant_id}"}
        store.decide_grant(conn, grant_id, status=status, by=sid)
        # Tell the requester, if that session is still around.
        requester = g.get("requested_by")
        if requester:
            store.post_message(
                conn,
                from_session=sid,
                from_handle=me_handle,
                body=f"access request #{grant_id} ({g['capability']} {g['resource_kind']} "
                f"'{g['resource']}') → {status}",
                to_session=requester,
                project=_project(conn, sid),
                urgent=(status != "granted"),
            )
        conn.commit()
        return {"ok": True, "grant_id": grant_id, "status": status}


@mcp.tool()
def grants(status: str | None = None, mine: bool = True) -> list[dict[str, Any]]:
    """List access grants and pending requests. By default those your project
    requested (`mine`); set mine=False to see requests aimed AT your project
    (what you may `grant`/`deny`). Filter by status
    (requested|granted|denied|revoked).
    """
    with store.connect() as conn:
        sid = _me(conn)
        my_project = (_project_name(conn, sid) or "").strip().lower()
        if mine:
            # Asked by my project, or by me before I moved to another project (#52).
            by_id: dict[int, dict[str, Any]] = {}
            for r in (
                *store.list_grants(conn, grantee=my_project or None, status=status),
                *store.list_grants(conn, requested_by=sid, status=status),
            ):
                by_id[r["id"]] = r
            rows = sorted(by_id.values(), key=lambda r: -r["id"])
        else:
            # Requests whose target is my project (a project resource aimed at me).
            rows = [
                r
                for r in store.list_grants(conn, resource_kind="project", status=status)
                if store._norm_grant_resource("project", r["resource"]) == my_project
            ]
        return rows


@mcp.tool()
def gate_topic(topic: str, gated: bool = True) -> dict[str, Any]:
    """Make a topic private (gated=True) or open (gated=False). Gating a room
    means a `listen`/`send` grant is required to join or post; gating it grants
    YOUR project both so you keep access, and subscribes you. Others reach it via
    `request_access` (issue #26).
    """
    topic = topic.lstrip("#")
    with store.connect() as conn:
        sid = _me(conn)
        my = (_project_name(conn, sid) or "").strip().lower()
        store.set_topic_gated(conn, topic, gated)
        if gated and my:
            for cap in ("listen", "send"):
                store.upsert_grant(
                    conn,
                    grantee=my,
                    resource_kind="topic",
                    resource=topic,
                    capability=cap,
                    status="granted",
                    by=sid,
                )
            store.subscribe(conn, sid, topic)
        conn.commit()
        return {"ok": True, "topic": topic, "gated": gated}


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #
@mcp.tool()
def send(
    body: str,
    to: str | None = None,
    topic: str | None = None,
    urgent: bool = False,
    reply_to: str | None = None,
) -> dict[str, Any]:
    """Send a message to the bus.

    Provide EITHER `to` (a session id or @handle for a direct message) OR
    `topic` (a channel name). If you give neither, it posts to your project's
    channel (#proj:<name>) so agents on the same repo see it. Set urgent=True to
    flag it. Messages are delivered as unread; recipients must call `read`.

    To answer a question that was asked of your role, pass `reply_to` with the
    question's guid (the `question` field of the inbox item). The answer is a
    direct message to the asker, so `to` is optional. Only a session that holds
    the question's role may answer it; the first answer removes the question from
    the other holders' inboxes.
    """
    with store.connect() as conn:
        sid = _me(conn)
        me_handle = _handle(conn, sid)
        project = _project(conn, sid)

        if reply_to is not None:
            q = questions.get(conn, reply_to)
            if q is None:
                return {"ok": False, "error": "reply_to is not a question guid"}
            mine = store.get_session(conn, sid)
            if not mine or store.role_key(mine["role"] or "") != q.role:
                return {"ok": False, "error": f"you do not hold the role '{q.role}'"}
            # An answer goes to the asker, or the asker never hears it.
            if topic is not None or (to is not None and _resolve_recipient(conn, to) != q.asked_by):
                return {"ok": False, "error": "an answer goes to the asker; omit `to` and `topic`"}
            to = q.asked_by

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

        # Cross-project consent (issue #26). Reaching another project — by DM, by
        # its channel, or through a gated topic — needs your own project, a
        # `--link` peer, or a granted `send` row. Open topics stay open.
        my_project = _project_name(conn, sid)
        if to_session is not None:
            trow = store.get_session(conn, to_session)
            tname = (trow["project_name"] if trow else None) or ""
            # A paired machine's agent is reachable from anywhere: the pairing is
            # the consent, and its project name means nothing on this machine.
            remote = trow is not None and _node(trow) is not None
            # Two members of the same chatroom have both opted in to talk.
            roommates = store.share_a_room(conn, sid, to_session)
            if not (remote or roommates) and not _may_reach_project(
                conn, my_project, tname, "send", sid
            ):
                return {
                    "ok": False,
                    "error": f"'{to}' is on project '{tname.strip().lower()}', not "
                    f"reachable from yours ('{(my_project or '').strip().lower()}') — use `request_access` "
                    "to ask for a grant",
                }
        else:
            pj = _proj_topic_name(conn, topic)
            if pj is not None:
                if not _may_reach_project(conn, my_project, pj, "send", sid):
                    return {
                        "ok": False,
                        "error": f"#{topic} is project '{pj}'s channel, not reachable "
                        f"from yours ('{(my_project or '').strip().lower()}') — use `request_access` "
                        "to ask for a grant",
                    }
            elif topic and store.topic_is_gated(conn, topic) and (my_project or "").strip():
                mine = (my_project or "").strip().lower()
                subscribed = topic in store.subscriptions_of(conn, sid)
                if not subscribed and not store.has_grant(conn, mine, "topic", topic, "send"):
                    return {
                        "ok": False,
                        "error": f"#{topic} is a gated topic — `request_access` for a "
                        f"'send' grant (or join it with a 'listen' grant first)",
                    }

        mid = store.post_message(
            conn,
            from_session=sid,
            from_handle=me_handle,
            body=body,
            topic=topic,
            to_session=to_session,
            reply_to=reply_to,
            project=project,
            urgent=urgent,
        )
        dest = f"@{to}" if to_session else f"#{topic}"
        return {"ok": True, "message_id": mid, "to": dest}


@mcp.tool()
def send_to_role(
    role: str, body: str, urgent: bool = False, question: bool = False
) -> dict[str, Any]:
    """Route a message by function: DM every active agent whose role is `role`,
    instead of naming a handle. This is how you hand work to whoever owns a
    domain (e.g. send_to_role("reviewer", "please review #123")) without knowing
    who is on shift. Delivers one DM per matching agent; yourself is excluded,
    and only agents on your project or a linked one are reached. Returns the
    recipients it hit — an empty list means no one currently holds that role.

    With question=True it posts ONE question addressed to the role instead of
    one DM per holder. Every holder sees it until one of them answers; if nobody
    holds the role yet, it waits for the first session that takes it. Same
    project only. Returns the question's guid; track it with `questions`.
    """
    with store.connect() as conn:
        sid = _me(conn)
        me_handle = _handle(conn, sid)
        project = _project(conn, sid)
        my_project = _project_name(conn, sid)
        if question:
            key = store.role_key(role)
            if not key:
                return {"ok": False, "error": "role is empty"}
            # The project key, not the path: paths differ between machines.
            mid = store.post_message(
                conn,
                from_session=sid,
                from_handle=me_handle,
                body=body,
                to_role=key,
                project=cloud.project_key(project) if project else None,
                urgent=urgent,
            )
            row = conn.execute("SELECT guid FROM messages WHERE id=?", (mid,)).fetchone()
            return {"ok": True, "role": key, "question": row["guid"]}
        recipients: list[dict[str, Any]] = []
        for r in store.agents_with_role(conn, role, exclude=sid):
            tname = (r["project_name"] or "").strip().lower()
            if not _may_reach_project(conn, my_project, tname, "send", sid):
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


@mcp.tool(name="questions")
def questions_tool(open_only: bool = True) -> dict[str, Any]:
    """List the questions you asked a role, and the ones waiting for your role.

    `asked` are questions you posted with send_to_role(question=True); each shows
    whether it is answered and by whom. `waiting` are unanswered questions
    addressed to the role you hold; answer one with send(body, reply_to=<guid>).
    """
    with store.connect() as conn:
        sid = _me(conn)
        mine = store.get_session(conn, sid)
        role = (mine["role"] if mine else None) or ""
        asked = [
            {
                "question": q.guid,
                "role": q.role,
                "body": q.body,
                "answered_by": q.answered_by.by_handle or q.answered_by.by_session
                if q.answered_by
                else None,
            }
            for q in questions.asked_by(conn, sid, open_only=open_only)
        ]
        waiting = [
            {"question": q.guid, "from": q.asked_handle or q.asked_by, "body": q.body}
            for q in (
                questions.open_for_role(conn, role, store.role_key(_project_name(conn, sid) or ""))
                if role.strip()
                else []
            )
            if q.asked_by != sid
        ]
        return {"asked": asked, "waiting": waiting}


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
        # A local agent wins over a paired machine's agent with the same handle.
        "SELECT session_id FROM sessions WHERE handle=? AND status='active' "
        "ORDER BY client LIKE 'lan:%', last_seen_at DESC LIMIT 1",
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
    mark_read: bool = False,
) -> dict[str, Any]:
    """Read messages addressed to you (direct messages + channels you follow).
    Defaults to unread only. It does not mark anything read unless
    mark_read=True, which marks exactly the returned messages read in the same
    call (one call instead of `inbox` + `read`). A truncated preview marked read
    is still readable with `get_message`.

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
                "to": (
                    "you"
                    if m["to_session"]
                    else f"role:{m['to_role']}"
                    if m.get("to_role")
                    else f"#{m['topic']}"
                ),
                "at": m["created_at"],
            }
            if m.get("to_role"):
                item["question"] = m["guid"]  # answer with send(body, reply_to=<this>)
            if m.get("reply_to"):
                item["reply_to"] = m["reply_to"]
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
        if mark_read and msgs:
            store.mark_read(conn, sid, message_ids=[m["id"] for m in msgs])
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
    ids: list[int] | None = None,
    id: int | None = None,
) -> dict[str, Any]:
    """Explicitly mark messages as read (they are never auto-read). Pass
    specific `message_ids`, or a `topic` to clear a channel, or all=True to
    clear your whole inbox. Returns how many were newly marked.

    `ids` (and `id` for a single message) are accepted as aliases for
    `message_ids`, since `inbox` returns each item's `id` — a mismatched name
    used to silently no-op (issue #23).
    """
    # Fold the aliases into one id list, de-duplicated and order-stable.
    merged: list[int] = []
    for src in (message_ids, ids):
        if src:
            merged.extend(src)
    if id is not None:
        merged.append(id)
    seen: set[int] = set()
    id_list = [m for m in merged if not (m in seen or seen.add(m))] or None

    with store.connect() as conn:
        sid = _me(conn)
        n = store.mark_read(
            conn,
            sid,
            message_ids=id_list,
            topic=topic.lstrip("#") if topic else None,
            all_unread=all,
        )
        remaining = store.unread_summary(conn, sid)["count"]
        result: dict[str, Any] = {"ok": True, "marked_read": n, "unread_remaining": remaining}
        # A zero is otherwise indistinguishable from success; say why (issue #23).
        if n == 0:
            if not (id_list or topic or all):
                result["note"] = (
                    "nothing to do — pass message_ids (or ids/id), a topic, or all=True"
                )
            else:
                result["note"] = "no match — those ids were already read or are not in your inbox"
        return result


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
        pj = _proj_topic_name(conn, topic)
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
def peers() -> list[dict[str, Any]]:
    """Other machines this bus syncs with, and the last pull from each.

    `transport` is "lan" (paired with `request("pair")`) or "wan" (a peer over a tunnel, set
    by the human in the machine-wide config). The daemon runs both. There is no CLI step.
    """
    from dataclasses import asdict

    from . import lan, pair, wan

    status = lan.load_status()
    paired = [
        {
            "transport": "lan",
            "node": node,
            "address": f"{peer.host}:{peer.port}",
            "last_pull": asdict(status[node]) if node in status else None,
        }
        for node, peer in pair.load_peers().items()
    ]
    return [*paired, *wan.peers_report()]


# --------------------------------------------------------------------------- #
# GitLab CI of the repo this session works in
# --------------------------------------------------------------------------- #
def _ci_repo():
    from . import ci

    return ci, ci.repo_here(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _human_allows(what: str) -> bool:
    """Ask the human with a desktop popup; True only on an explicit Allow."""
    from . import desktop

    answer = desktop.ask("cabildo: CI request", what, ["Allow", "Deny"], timeout_ms=120_000)
    return answer == "Allow"


@mcp.tool()
def ci_status(ref: str | None = None, pipeline_id: int | None = None) -> dict[str, Any]:
    """The newest pipeline (GitLab) or workflow run (GitHub) of this repo's ``ref`` (default: the pipeline of HEAD) and
    each job's status. ``passed`` is True, False, or None while it still runs.
    """
    try:
        ci, repo = _ci_repo()
        sha = None
        if not (ref or pipeline_id):
            sha = (
                subprocess.run(
                    ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
                ).stdout.strip()
                or None
            )
        p = ci.pipeline(repo, sha=sha, ref=ref, pipeline_id=pipeline_id)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if p is None:
        return {"ok": False, "error": "no pipeline"}
    passed = {ci.Exit.PASSED: True, ci.Exit.FAILED: False}.get(p.exit)
    return {"ok": True, "passed": passed, **ci.as_dict(p)}


@mcp.tool()
def ci_retry(job_id: int) -> dict[str, Any]:
    """Run a finished CI job of this repo again (GitLab or GitHub), e.g. a failed test job."""
    try:
        ci, repo = _ci_repo()
        job = ci.retry(repo, job_id)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "job": job.__dict__}


@mcp.tool()
def ci_run(ref: str, workflow: str | None = None) -> dict[str, Any]:
    """Start a new pipeline on ``ref`` (GitHub: dispatch ``workflow``, e.g. ``ci.yml``).

    The human approves it in a popup first.
    """
    try:
        ci, repo = _ci_repo()
        if not _human_allows(f"Start a pipeline on {repo.path}@{ref}?"):
            return {"ok": False, "error": "the human did not allow it"}
        p = ci.run(repo, ref, workflow=workflow)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **ci.as_dict(p)}


@mcp.tool()
def ci_play(job_id: int) -> dict[str, Any]:
    """Start a manual job (often a deploy); on GitHub, approve the waiting deployments of run
    ``job_id``. The human approves it in a popup first.
    """
    try:
        ci, repo = _ci_repo()
        if not _human_allows(f"Run manual job {job_id} on {repo.path}?"):
            return {"ok": False, "error": "the human did not allow it"}
        job = ci.play(repo, job_id)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "job": job.__dict__}


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
      pair  — pair this machine with another on the LAN so messages sync both
              ways. No params: get a 6-digit PIN to show the user. On the other
              machine: {"pin": "482913"}, plus "host" and "port" if it is not
              found over mDNS. The PIN is valid for 10 minutes and 5 tries.

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


@mcp.tool()
def standup(hours: float = 24.0) -> str:
    """Who touched what over the last `hours`: messages, tasks in flight (claim = flow),
    spawns, active agents, and token cost — from cabildo's own record, not scraped chat.
    """
    from . import standup as _standup

    with store.connect() as conn:
        return _standup.render(_standup.collect(conn, hours=hours))


# --------------------------------------------------------------------------- #
# Claude Code sessions: find, read, tag, create, trash (#44)
# --------------------------------------------------------------------------- #
def _sessions_scope(all_projects: bool) -> tuple[str, str | None]:
    """The caller's session id and the folder its session tools see (None: all)."""
    with store.connect() as conn:
        sid = _me(conn)
        row = store.get_session(conn, sid)
    root = (row["project"] or row["cwd"]) if row else None
    return sid, None if all_projects else (root or os.getcwd())


def _sessions_select(
    ids: list[str] | None, tag: str | None, all_projects: bool
) -> tuple[str, str | None, list[Any]]:
    from . import sessions_ops

    sid, root = _sessions_scope(all_projects)
    return sid, root, sessions_ops.select(root, ids=ids or (), tag=tag)


@mcp.tool()
def sessions_focus(name: str) -> dict[str, Any]:
    """Make the cabildo workspace show a session: "take me to xxx".

    ``name`` is a session id or 8-char prefix, a robot name, an ``@handle`` or a part of the
    title or label. It works while a workspace (``cabildo workspace``) is running and lists
    that session; the workspace opens it, starts it again if it had ended, and gives it the
    keys. Ambiguous or unknown names change nothing and say why.
    """
    from .workspace import focus

    replies = focus.ask(name)
    if not replies:
        return {"ok": False, "error": "no cabildo workspace answered: is one running?"}
    shown = next((r for r in replies if r.ok), None)
    if shown is not None:
        return {"ok": True, "message": shown.message}
    return {"ok": False, "error": "; ".join(r.message for r in replies)}


@mcp.tool()
def sessions_list(
    tag: str | None = None,
    query: str | None = None,
    contains: str | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Find Claude Code sessions of this project: id, title, date, tags, cwd, first prompt.

    ``tag`` keeps those with that tag; ``query`` matches title, label, first prompt,
    robot name (``tiny-dynamo-616b8``), ``@handle`` and session id; ``contains``
    matches any user or assistant text. ``all_projects``
    searches every folder instead of this project and its subfolders. Read one
    with ``sessions_read``.
    """
    from . import sessions_ops

    _sid, root = _sessions_scope(all_projects)
    rows = sessions_ops.rows(root, tag=tag, query=query, contains=contains)
    return {
        "count": len(rows),
        "sessions": [
            sessions_ops.describe(r) | {"first_text": sessions_ops.first_text(r)} for r in rows
        ],
    }


@mcp.tool()
def sessions_read(
    id: str, offset: int = 0, limit: int = 40, all_projects: bool = False
) -> dict[str, Any]:
    """The user and assistant text of one session, oldest first, ``limit`` turns from
    ``offset``. Tool calls and text Claude Code injects are left out.
    """
    from . import sessions_ops

    try:
        _sid, _root, rows = _sessions_select([id], None, all_projects)
    except sessions_ops.SessionsError as e:
        return {"ok": False, "error": str(e)}
    turns = sessions_ops.conversation(rows[0])
    page = turns[max(offset, 0) : max(offset, 0) + max(limit, 1)]
    return {
        "ok": True,
        "session": sessions_ops.describe(rows[0]),
        "total": len(turns),
        "offset": offset,
        "turns": [{"role": t.role, "at": t.timestamp[:16], "text": t.text} for t in page],
    }


@mcp.tool()
def sessions_tag(
    ids: list[str],
    add: list[str] | None = None,
    remove: list[str] | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Add and remove tags on sessions (id, 8-char prefix, robot name or ``@handle`` each)."""
    from . import sessions_ops

    try:
        _sid, _root, rows = _sessions_select(ids, None, all_projects)
        changed = sessions_ops.tag(rows, add or (), remove or ())
    except sessions_ops.SessionsError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "changed": changed}


@mcp.tool()
def sessions_rename(
    id: str,
    title: str,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Give one session your own name (id, 8-char prefix, robot name or ``@handle``).

    An empty ``title`` removes the name, so Claude's own title shows again.
    ``sessions_list(query=...)`` finds a session by this name.
    """
    from . import sessions_ops

    try:
        _sid, _root, rows = _sessions_select([id], None, all_projects)
        shown = sessions_ops.rename(rows[0], title)
    except sessions_ops.SessionsError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "title": shown}


@mcp.tool()
def sessions_create(
    prompt: str,
    made_from: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Start a new session in this project whose first message is ``prompt`` (text you
    wrote, for example from sessions you read). It runs one headless turn now.

    ``made_from`` lists the sessions it replaces; only those can later go to
    ``sessions_trash``. Open it with the returned ``resume`` line.
    """
    from . import sessions_ops

    try:
        _sid, _root, rows = _sessions_select(made_from, None, False) if made_from else ("", "", [])
        root = _sessions_scope(False)[1] or os.getcwd()
        made = sessions_ops.create(
            root, prompt, made_from=[i for r in rows for i in r.ids], tags=tags or ()
        )
    except sessions_ops.SessionsError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "session_id": made.session_id, "resume": made.resume}


@mcp.tool()
def sessions_trash(
    ids: list[str] | None = None,
    tag: str | None = None,
    force: bool = False,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Move sessions (by ``ids`` or ``tag``) to the workspace trash. Reversible: RESTORE.txt.

    Refused for the calling session, and unless a session made with
    ``sessions_create`` lists all of them in ``made_from`` (``force`` skips only that
    check). Sessions made from others are left out.
    """
    from . import sessions_ops

    try:
        sid, _root, rows = _sessions_select(ids, tag, all_projects)
        dests = sessions_ops.trash(rows, keep=sid, force=force)
    except sessions_ops.SessionsError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "trashed": len(dests), "trash": [str(d) for d in dests]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
