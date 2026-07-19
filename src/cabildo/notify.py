"""Human-readable strings the hooks inject as additionalContext. Kept compact:
these land in the model's context every turn, so brevity matters."""

from __future__ import annotations

import sqlite3

from . import store


def unread_line(conn: sqlite3.Connection, session_id: str) -> str | None:
    """One-line unread notice for UserPromptSubmit. None when nothing is unread
    (so we inject nothing rather than noise)."""
    s = store.unread_summary(conn, session_id)
    if s["count"] == 0:
        return None
    parts: list[str] = []
    if s["dm"]:
        parts.append(f"{s['dm']} DM")
    topic_bits = ", ".join(f"#{t}({n})" for t, n in s["by_topic"].items())
    if topic_bits:
        parts.append(topic_bits)
    detail = " · ".join(parts) if parts else ""
    urgent = " ⚠ URGENT" if s["urgent"] else ""
    return (
        f"📬 cabildo: {s['count']} unread{urgent}"
        + (f" — {detail}" if detail else "")
        + ". Use `inbox` to read, then `read` to mark them."
    )


def midturn_urgent_text(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Mid-turn (PostToolUse) delivery of URGENT mail. Injects each message
    exactly once — surfaced ids are recorded as delivered so the next tool call
    stays silent. Ordinary mail never interrupts a turn; it waits for the
    UserPromptSubmit notice."""
    msgs = store.undelivered_urgent(conn, session_id)
    if not msgs:
        return None
    lines = [f"⚠ cabildo: {len(msgs)} URGENT message(s) just arrived:"]
    for m in msgs:
        dest = "DM" if m["to_session"] else f"#{m['topic']}"
        preview = " ".join(m["body"].split())[:200]
        lines.append(f"  · {m['from_display']} → {dest} (id {m['id']}): {preview}")
    lines.append(
        "If this affects what you are doing, act on it now (reply via `send`); "
        "use `inbox` for full text and `read` to mark them."
    )
    store.mark_delivered(conn, session_id, [m["id"] for m in msgs])
    return "\n".join(lines)


def urgent_block_reason(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Reason string for the Stop hook to refuse an idle-out. None unless there
    is unread *urgent* mail — ordinary messages must never hold a turn open."""
    urgent = [m for m in store.inbox(conn, session_id, unread_only=True, limit=20)
              if m["urgent"]]
    if not urgent:
        return None
    lines = [
        f"⚠ You have {len(urgent)} unread URGENT cabildo message(s). Delivery is "
        "pull-based, so going idle now means nobody sees your answer:"
    ]
    for m in urgent[:5]:
        preview = " ".join(m["body"].split())[:160]
        dest = "DM" if m["to_session"] else f"#{m['topic']}"
        lines.append(f"  · {m['from_display']} → {dest} (id {m['id']}): {preview}…")
    lines.append(
        "Call `inbox` to read them in full, act or reply via `send` if they need "
        "an answer, then `read` to mark them. Marking them read is what lets this "
        "turn end — otherwise you will be stopped again next turn."
    )
    return "\n".join(lines)


def session_banner(conn: sqlite3.Connection, session_id: str) -> str:
    """Fuller status for SessionStart: who you are, who else is online on this
    project, active locks, unread count."""
    me = store.get_session(conn, session_id)
    name = store.display_name(me, session_id)
    project = me["project"] if me else None
    project_name = me["project_name"] if me else None

    lines = [f"🔌 cabildo online — you are {name} (session {session_id[:8]})."]
    if project_name:
        lines.append(f"Project: {project_name} · channel #{store.project_topic(project_name)}")

    peers = store.active_sessions(conn, project=project, exclude=session_id)
    if peers:
        who = ", ".join(
            f"{store.display_name(p)}" + (f" ({p['project_name']})" if not project else "")
            for p in peers[:8]
        )
        lines.append(f"{len(peers)} other agent(s) here: {who}")
    else:
        lines.append("No other agents currently active on this project.")

    claims = store.list_claims(conn, project=project)
    if claims:
        lines.append(
            "Active locks: "
            + ", ".join(f"{c['resource']} by {c['holder']}" for c in claims[:6])
        )

    s = store.unread_summary(conn, session_id)
    if s["count"]:
        lines.append(f"📬 {s['count']} unread message(s) — call `inbox`.")

    lines.append(
        "Coordinate with other agents via: send / inbox / read / list_agents / "
        "claim / release / claims. Messages are NOT auto-read — mark them with `read`."
    )
    return "\n".join(lines)
