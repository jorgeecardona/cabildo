"""Human-readable strings the hooks inject as additionalContext. Kept compact:
these land in the model's context every turn, so brevity matters.
"""

from __future__ import annotations

import pathlib
import sqlite3

from . import store


def unread_line(conn: sqlite3.Connection, session_id: str) -> str | None:
    """One-line unread notice for UserPromptSubmit. None when nothing is unread
    (so we inject nothing rather than noise).
    """
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
    UserPromptSubmit notice.
    """
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
    is unread *urgent* mail — ordinary messages must never hold a turn open.
    """
    urgent = [m for m in store.inbox(conn, session_id, unread_only=True, limit=20) if m["urgent"]]
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


_CONDITION_MARK = {"critical": "🚨", "warn": "⚠", "info": "ℹ"}


def condition_text(conn: sqlite3.Connection, session_id: str, *, mark: bool = True) -> str | None:
    """Machine-wide conditions this session hasn't been told about yet.

    This is the nudge channel for things no agent can see from inside its own
    turn: the box is out of RAM, dead agents left processes running, CI is down.
    Injected once per episode — a condition that merely persists says nothing on
    the next tool call, which is what makes it affordable to check every time.

    The `by_session` payload is what stops this being ambient anxiety. Every
    agent reads the same headline, but the one actually holding the memory is
    told so, in the second line, and only it is being asked to act.
    """
    rows = store.undelivered_conditions(conn, session_id)
    if not rows:
        return None

    lines: list[str] = []
    for c in rows:
        if c["state"] == "cleared":
            lines.append(f"✅ cabildo · {c['key']} cleared: {c['headline']}")
            continue
        lines.append(
            f"{_CONDITION_MARK.get(c['level'], '⚠')} cabildo alert · "
            f"{c['key']} [{c['level']}]: {c['headline']}"
        )
        mine = (c["data"].get("by_session") or {}).get(session_id)
        if mine:
            what = ", ".join(f"{p['what']} ({p['rss_mb']}M)" for p in mine.get("procs", []))
            lines.append(
                f"   YOU are holding {mine['rss_mb'] / 1024:.1f}G in "
                f"{mine['count']} process(es){': ' + what if what else ''}"
            )
        for p in (c["data"].get("procs") or [])[:4]:
            lines.append(
                f"   · pid {p['pid']} {p['what']} ({p['rss_mb']}M, {p['age']} old, from {p['who']})"
            )
        if c["advice"]:
            lines.append(f"   → {c['advice']}")

    if mark:
        store.mark_conditions_delivered(conn, session_id, rows)
    return "\n".join(lines)


def _join(*parts: str | None) -> str | None:
    """Stitch several injections into one additionalContext blob, dropping the
    silent ones. Hooks emit at most one string, so anything with something to
    say has to share the slot.
    """
    kept = [p for p in parts if p]
    return "\n".join(kept) if kept else None


def register_line(cwd: str | None, handle: str | None = None) -> str | None:
    """One line about this project's issue register, or None when it has none.

    Measured, not guessed: over the first five hours across three repos, agents called `file_issue`
    thirteen times and `board` four. They use the register to record and almost never to plan —
    reasonably, since each arrives already told what to do. Naming the tool "start here" in its own
    description did not change that, because a tool description is read when you are already
    choosing a tool.

    So the cue goes where an agent looks before it has chosen anything, states the count so the
    board is a fact rather than an invitation, and names the top-ranked item so "there is ordered
    work here" is concrete. Silent when there is no register, which is most projects.
    """
    if not cwd:
        return None
    try:
        from . import issues

        folder = issues.find_folder(pathlib.Path(cwd))
        if folder is None:
            return None
        config = issues.load_config(folder)
        board = issues.board(folder, config, owner=handle)
    except Exception:
        return None  # a broken register must never cost anyone a session
    if not (board.ready or board.mine or board.waiting):
        return None

    bits = [f"{len(board.ready)} ready"]
    if board.waiting:
        bits.append(f"{len(board.waiting)} waiting")
    if board.mine:
        bits.append(f"{len(board.mine)} yours")
    line = f"📋 register: {', '.join(bits)} — `board` to plan, `file_issue` for what you "
    line += "find and won't fix now."
    if board.ready:
        top = board.ready[0]
        rank = config.ranking
        mark = ""
        if rank is not None:
            value = top.extra.get(rank.name)
            if isinstance(value, str):
                mark = f"[{value}] "
        # The top item by name, because a count is an invitation and a title is a fact. The slug is
        # what `show_issue`/`claim_issue` take, so this line can be acted on without a lookup.
        line += f"\n   next: {mark}{top.title[:64]} — {top.slug}"
    return line


def session_banner(conn: sqlite3.Connection, session_id: str) -> str:
    """Fuller status for SessionStart: who you are, who else is online on this
    project, active locks, unread count.
    """
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
            "Active locks: " + ", ".join(f"{c['resource']} by {c['holder']}" for c in claims[:6])
        )

    s = store.unread_summary(conn, session_id)
    if s["count"]:
        lines.append(f"📬 {s['count']} unread message(s) — call `inbox`.")

    reg = register_line(me["cwd"] if me else None, me["handle"] if me else None)
    if reg:
        lines.append(reg)

    lines.append(
        "Coordinate with other agents via: send / inbox / read / list_agents / "
        "claim / release / claims. Messages are NOT auto-read — mark them with `read`."
    )
    # Only when there is someone to collide with. Told at SessionStart because
    # by the time an agent's `git push` is denied it has already committed, and
    # an agent that learns the rule from a refusal spends its next move
    # improvising a workaround instead of queueing.
    if peers:
        lines.append(
            "⚠ Shared repo. Do NOT `git push` — it races them and the guard will "
            "deny it. Commit, then queue "
            '`request(action="land", params={"cwd": ..., "target": "main"})`: the '
            "daemon rebases, gates, pushes and verifies one agent at a time. "
            "Stage explicit paths — `git add -A` in a shared checkout commits "
            "their unfinished work."
        )
    # Last, and marked delivered here: a session starting onto a machine that is
    # already out of RAM should learn it before its first tool call, not after
    # the build it was about to launch.
    conditions = condition_text(conn, session_id)
    if conditions:
        lines.append(conditions)
    return "\n".join(lines)
