"""Standup: who touched what over a window, from cabildo's own record — messages,
task claims (claim = flow), spawns, and token cost — instead of scraped chat.

Read-only: it never changes state. Same data the viewer and the agenda draw on, so
what the standup says an agent did is what actually crossed the bus.
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
        "messages": {"total": len(msgs), "by_sender": dict(by_sender.most_common())},
        "tasks_in_flight": tasks,  # slug -> holder handle
        "spawns": len(spawns),
        "agents_active": [store.display_name(a) for a in store.active_sessions(conn)],
        "cost_weighted": round(cost.get("totals", {}).get("weighted", 0.0), 1),
    }


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
    lines.append(f"  spawned: {data['spawns']}")
    tasks = data["tasks_in_flight"]
    if tasks:
        lines.append(f"  tasks in flight ({len(tasks)}):")
        lines += [f"    {slug} — {holder}" for slug, holder in list(tasks.items())[:12]]
    else:
        lines.append("  tasks in flight: none claimed")
    lines.append(f"  cost (weighted): {data['cost_weighted']:g}")
    return "\n".join(lines)
