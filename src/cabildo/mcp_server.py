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


def _may_reach_project(
    conn, my_project: str | None, target_project: str | None, capability: str
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
    return store.has_grant(conn, mine, "project", tgt, capability)


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
def note_add(text: str, type: str = "topic") -> dict[str, Any]:
    """Capture a loose end for THIS session (issue #34): a topic you just opened,
    a follow-up you will not do now, something half-finished. Add one the moment a
    new thread starts, so it is not lost when the conversation moves on — `/wrap-up`
    reads these back instead of re-reading the whole conversation. `type` is
    topic | need | bug | idea | unfinished. The note starts `open`; close it with
    `note_done` when you finish it here, or `note_park` when you promote it to a
    project issue/agenda item. List them with `notes`.
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
    a project issue or agenda item so a later session picks it up without
    copy-paste. File that issue first (`file_issue` / `agenda_add`) and pass its
    name as `ref` so the note records where it went. See `notes` for the ids.
    """
    with store.connect() as conn:
        _me(conn)
        row = store.set_note_state(conn, id, state="parked", parked_ref=ref)
        return {"ok": bool(row), "id": id, "note": row}


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
        my_project = _project_name(conn, sid)
        pj = _proj_topic_name(topic)
        if pj is not None and not _may_reach_project(conn, my_project, pj, "listen"):
            return {
                "ok": False,
                "error": f"#{topic} is project '{pj}'s private channel, not reachable "
                f"from yours — `request_access` for a grant, or "
                f"`cabildo project <yours> --link {pj}`",
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
            rows = store.list_grants(conn, grantee=my_project or None, status=status)
        else:
            # Requests whose target is my project (a project resource aimed at me).
            rows = [
                r
                for r in store.list_grants(conn, resource_kind="project", status=status)
                if r["resource"] == my_project
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

        # Cross-project consent (issue #26). Reaching another project — by DM, by
        # its channel, or through a gated topic — needs your own project, a
        # `--link` peer, or a granted `send` row. Open topics stay open.
        my_project = _project_name(conn, sid)
        if to_session is not None:
            trow = store.get_session(conn, to_session)
            tname = (trow["project_name"] if trow else None) or ""
            if not _may_reach_project(conn, my_project, tname, "send"):
                return {
                    "ok": False,
                    "error": f"'{to}' is on project '{tname.strip().lower()}', not "
                    f"reachable from yours — `request_access` for a grant, or "
                    f"`cabildo project <yours> --link {tname.strip().lower()}`",
                }
        else:
            pj = _proj_topic_name(topic)
            if pj is not None:
                if not _may_reach_project(conn, my_project, pj, "send"):
                    return {
                        "ok": False,
                        "error": f"#{topic} is project '{pj}'s channel, not reachable "
                        f"from yours — `request_access` for a grant, or "
                        f"`cabildo project <yours> --link {pj}`",
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
        my_project = _project_name(conn, sid)
        recipients: list[dict[str, Any]] = []
        for r in store.agents_with_role(conn, role, exclude=sid):
            tname = (r["project_name"] or "").strip().lower()
            if not _may_reach_project(conn, my_project, tname, "send"):
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
      pair  — pair this machine with another on the LAN so messages sync both
              ways. No params: get a 6-digit PIN to show the user. On the other
              machine: {"pin": "482913"}, plus "host" and "port" if it is not
              found by broadcast. The PIN allows one attempt within 120 s.

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
