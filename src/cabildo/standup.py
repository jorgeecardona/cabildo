"""Standup: who touched what over a window, from cabildo's own record — messages,
task claims (claim = flow), spawns, and token cost — instead of scraped chat.

Read-only: it never changes state. Same data the viewer draws on, so what the
standup says an agent did is what actually crossed the bus.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import store, usage

DEFAULT_HOURS = 24


def collect(conn, *, hours: float = DEFAULT_HOURS) -> dict[str, Any]:
    """Aggregate the last `hours` of bus activity into a structured standup."""
    since = store._now() - hours * 3600
    msgs = [
        store._msg_dict(r)
        for r in conn.execute(
            "SELECT * FROM messages WHERE created_at >= ? ORDER BY created_at", (since,)
        )
    ]
    by_sender = Counter(m["from_display"] for m in msgs)
    tasks = {
        c["resource"][len("issue:") :]: c["holder"]
        for c in store.list_claims(conn)
        if c["resource"].startswith("issue:")
    }
    spawns = [s for s in store.list_spawns(conn, limit=500) if s.get("created_at", 0) >= since]
    cost = usage.summarize(since)
    return {
        "hours": hours,
        "collaboration": _collaboration(conn, since),
        "messages": {"total": len(msgs), "by_sender": dict(by_sender.most_common())},
        "tasks_in_flight": tasks,  # slug -> holder handle
        "spawns": len(spawns),
        "agents_active": [store.display_name(a) for a in store.active_sessions(conn)],
        "cost_weighted": round(cost.get("totals", {}).get("weighted", 0.0), 1),
    }


def _collaboration(conn, since: float) -> dict[str, int]:
    """Agent-to-agent traffic in the window: DMs, distinct pairs, cross-project
    DMs, and push wakes (turns the push plugin started from a message).

    The human is left out: these numbers say whether agents talk to each other.
    """
    rows = conn.execute(
        "SELECT m.from_session AS a, m.to_session AS b, "
        "sa.project_name AS pa, sb.project_name AS pb "
        "FROM messages m "
        "LEFT JOIN sessions sa ON sa.session_id = m.from_session "
        "LEFT JOIN sessions sb ON sb.session_id = m.to_session "
        "WHERE m.created_at >= ? AND m.to_session IS NOT NULL "
        "AND m.from_session != ? AND m.to_session != ?",
        (since, store.HUMAN_SESSION, store.HUMAN_SESSION),
    ).fetchall()
    pairs = {frozenset((r["a"], r["b"])) for r in rows}
    cross = sum(1 for r in rows if r["pa"] and r["pb"] and r["pa"] != r["pb"])
    wakes = conn.execute(
        "SELECT COUNT(*) FROM hook_metrics WHERE event = 'push_wake' AND at >= ?", (since,)
    ).fetchone()[0]
    return {"dms": len(rows), "pairs": len(pairs), "cross_project": cross, "push_wakes": wakes}


def render(data: dict[str, Any]) -> str:
    """A tight, human-readable standup from `collect`'s output."""
    lines = [f"standup — last {data['hours']:g}h"]
    m = data["messages"]
    who = ", ".join(f"{k} {v}" for k, v in list(m["by_sender"].items())[:6])
    lines.append(f"  messages: {m['total']}" + (f" ({who})" if who else ""))
    agents = data["agents_active"]
    lines.append(
        f"  agents active: {len(agents)}" + (f" — {', '.join(agents[:8])}" if agents else "")
    )
    c = data["collaboration"]
    lines.append(
        f"  agent DMs: {c['dms']} between {c['pairs']} pair(s), "
        f"{c['cross_project']} cross-project; push wakes: {c['push_wakes']}"
    )
    lines.append(f"  spawned: {data['spawns']}")
    tasks = data["tasks_in_flight"]
    if tasks:
        lines.append(f"  tasks in flight ({len(tasks)}):")
        lines += [f"    {slug} — {holder}" for slug, holder in list(tasks.items())[:12]]
    else:
        lines.append("  tasks in flight: none claimed")
    lines.append(f"  cost (weighted): {data['cost_weighted']:g}")
    return "\n".join(lines)
