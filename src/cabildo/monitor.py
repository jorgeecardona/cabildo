"""Turns a machine reading into a condition the fleet can act on.

The daemon samples; this module decides whether that sample is news. Two
conditions live here:

  * **mem** — the host is running out of memory. Everyone hears it, and the
    agent that is actually holding the memory hears *that*, because the sample
    carries a per-session breakdown along with it.
  * **orphans** — processes whose owning agent is gone. Waste, not pressure:
    it fires whether or not the box is short of RAM, and the fix is different
    (kill something nobody wants, rather than defer something someone does).

Two rules of taste that the thresholds exist to serve:

**Hysteresis.** A machine hovering at the line would otherwise flap
warn/clear/warn every sweep, and every flap costs every session a context
injection. Recovery has to clear the threshold by a margin before we call it
recovery.

**PSI outranks percentages.** "Available RAM" is a poor proxy for suffering: a
box can sit at 5% available and be fine, or thrash at 25%. `full avg60` from
/proc/pressure/memory is the fraction of the last minute in which every runnable
task was stalled on memory — the machine actually being unusable. When the
kernel offers that number it can raise the level on its own.

Nothing here kills a process. The most this mechanism ever does is tell an agent
what is true and let it decide.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import procs, store

MEM_KEY = "mem"
ORPHAN_KEY = "orphans"


@dataclass(frozen=True)
class Thresholds:
    warn_pct: float = 15.0  # MemAvailable below this → warn
    critical_pct: float = 7.0  # …and below this → critical
    clear_margin: float = 5.0  # points of recovery required to relax a level
    psi_warn: float = 10.0  # full avg60: a tenth of the minute fully stalled
    psi_critical: float = 25.0
    orphan_rss_mb: int = 250  # per-process floor: below it, not worth a word
    orphan_total_mb: int = 750  # total floor: one stray 300 MB process is life


def _gb(kb: int) -> str:
    g = kb / 1048576.0
    return f"{g:.1f}G" if g < 100 else f"{g:.0f}G"


def _mins(seconds: float) -> str:
    m = seconds / 60.0
    if m < 60:
        return f"{m:.0f}m"
    return f"{m / 60:.1f}h"


# --------------------------------------------------------------------------- #
# Level
# --------------------------------------------------------------------------- #
def level_for(snap: procs.Snapshot, th: Thresholds, *, sticky: str | None = None) -> str | None:
    """The level this reading deserves, or None for "nothing to say".

    `sticky` is the level currently raised. Passing it applies the hysteresis
    margin to every threshold at or below it, so getting *out* of a level costs
    more than getting into it. PSI is exempt: it is already a 60-second average,
    so it does not flap and does not need damping.
    """
    mem, stall = snap.memory, snap.stall_pct
    if not mem.ok:
        return None
    m = th.clear_margin
    crit_at = th.critical_pct + (m if sticky == "critical" else 0.0)
    warn_at = th.warn_pct + (m if sticky in ("warn", "critical") else 0.0)

    if mem.avail_pct <= crit_at or stall >= th.psi_critical:
        return "critical"
    if mem.avail_pct <= warn_at or stall >= th.psi_warn:
        return "warn"
    return None


# --------------------------------------------------------------------------- #
# Condition payloads
# --------------------------------------------------------------------------- #
def mem_headline(snap: procs.Snapshot) -> str:
    mem = snap.memory
    bits = [f"{mem.avail_pct:.0f}% RAM available ({_gb(mem.available_kb)} of {_gb(mem.total_kb)})"]
    if mem.swap_total_kb and mem.swap_used_pct >= 20:
        bits.append(f"swap {mem.swap_used_pct:.0f}% of {_gb(mem.swap_total_kb)} used")
    if snap.stall_pct >= 1:
        bits.append(f"{snap.stall_pct:.0f}% of the last minute stalled on memory")
    return " · ".join(bits)


ADVICE = {
    "critical": (
        "Do not start builds, test suites, or parallel work right now, and do not "
        "spawn agents. Finish or kill what you already started — if the line above "
        "says you are holding memory, that is yours to release. `cabildo mem` shows "
        "who holds what."
    ),
    "warn": (
        "Be deliberate with memory: one heavy thing at a time, no parallel builds or "
        "test suites, and close what you are done with. `cabildo mem` shows who holds "
        "what."
    ),
}


def mem_payload(snap: procs.Snapshot, conn, *, limit: int = 3) -> dict:
    """The per-session breakdown that rides along with the mem condition.

    Computed once, by the producer, for the whole fleet — the alternative is
    every session scanning /proc on every tool call to answer a question the
    daemon already answered. Each hook then does one dict lookup on its own id.
    """
    out: dict[str, dict] = {}
    for sid, plist in snap.by_session().items():
        plist = sorted(plist, key=lambda p: p.rss_kb, reverse=True)
        out[sid] = {
            "rss_mb": sum(p.rss_kb for p in plist) // 1024,
            "count": len(plist),
            "procs": [{"pid": p.pid, "rss_mb": p.rss_mb, "what": p.label()} for p in plist[:limit]],
        }
    top = [
        {
            "rss_mb": p.rss_mb,
            "what": p.label(40),
            "who": store.display_name(store.get_session(conn, p.session_id), p.session_id)
            if p.session_id
            else None,
        }
        for p in snap.procs[:5]
    ]
    return {
        "by_session": out,
        "top": top,
        "avail_pct": round(snap.memory.avail_pct, 1),
        "total_gb": round(snap.memory.total_kb / 1048576.0, 1),
        "stall_pct": snap.stall_pct,
    }


def orphan_payload(loose: list[procs.Proc], conn, *, limit: int = 6) -> dict:
    rows = []
    for p in sorted(loose, key=lambda p: p.rss_kb, reverse=True)[:limit]:
        who = (
            store.display_name(store.get_session(conn, p.session_id), p.session_id)
            if p.session_id
            else f"pid {p.claude_pid}"
        )
        rows.append(
            {
                "pid": p.pid,
                "rss_mb": p.rss_mb,
                "what": p.label(50),
                "who": who,
                "age": _mins(_age(p)),
            }
        )
    return {
        "procs": rows,
        "count": len(loose),
        "rss_mb": sum(p.rss_kb for p in loose) // 1024,
    }


def _age(p: procs.Proc) -> float:
    return max(0.0, time.time() - p.started_at) if p.started_at else 0.0


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def sweep(
    conn, th: Thresholds | None = None, *, source: str | None = None, min_rss_mb: int = 100
) -> list[str]:
    """Sample the machine and reconcile both conditions. Returns log lines for
    whatever was actually news — an unchanged condition is refreshed silently.
    """
    th = th or Thresholds()
    source = source or f"daemon:{store.hostname()}"

    live = store.active_sessions(conn)
    pid_to_session = {r["claude_pid"]: r["session_id"] for r in live if r["claude_pid"]}
    live_ids = {r["session_id"] for r in live}
    snap = procs.snapshot(min_rss_mb=min_rss_mb, pid_to_session=pid_to_session)

    log: list[str] = []
    log += _reconcile_mem(conn, snap, th, source)
    log += _reconcile_orphans(conn, snap, th, source, live_ids)
    store.prune_conditions(conn)
    return log


def _current(conn, key: str) -> dict | None:
    for c in store.active_conditions(conn):
        if c["key"] == key:
            return c
    return None


def _reconcile_mem(conn, snap: procs.Snapshot, th: Thresholds, source: str) -> list[str]:
    cur = _current(conn, MEM_KEY)
    level = level_for(snap, th, sticky=cur["level"] if cur else None)
    if level is None:
        if cur and store.clear_condition(
            conn,
            key=MEM_KEY,
            source=source,
            headline=f"memory recovered — {snap.memory.avail_pct:.0f}% RAM available again",
        ):
            return [f"✅ mem cleared ({snap.memory.avail_pct:.0f}% available)"]
        return []
    res = store.raise_condition(
        conn,
        key=MEM_KEY,
        level=level,
        headline=mem_headline(snap),
        advice=ADVICE.get(level),
        data=mem_payload(snap, conn),
        source=source,
    )
    return [f"⚠ mem {level}: {res['headline']}"] if res["announced"] else []


def _reconcile_orphans(
    conn, snap: procs.Snapshot, th: Thresholds, source: str, live_ids: set[str]
) -> list[str]:
    loose = [
        p
        for p in procs.loose(snap, live_ids)
        if p.rss_mb >= th.orphan_rss_mb and not procs.is_self(p)
    ]
    total_mb = sum(p.rss_mb for p in loose)
    cur = _current(conn, ORPHAN_KEY)

    if not loose or total_mb < th.orphan_total_mb:
        if cur and store.clear_condition(
            conn,
            key=ORPHAN_KEY,
            source=source,
            headline="the leftover processes are gone",
        ):
            return ["✅ orphans cleared"]
        return []

    # Level is about waste, not urgency: leftovers only become critical when
    # they are also a meaningful slice of a machine that is already short.
    level = "warn"
    if (
        snap.memory.ok
        and total_mb * 1024 > snap.memory.total_kb * 0.15
        and snap.memory.avail_pct <= th.warn_pct
    ):
        level = "critical"
    res = store.raise_condition(
        conn,
        key=ORPHAN_KEY,
        level=level,
        headline=(
            f"{len(loose)} process(es) holding {total_mb / 1024:.1f}G were left "
            f"behind by agents that are gone"
        ),
        advice=(
            "Nobody is waiting on these — the sessions that started them ended. "
            "`cabildo mem --loose` lists them with pids; kill the ones you "
            "recognize as yours. Do not kill what you cannot account for."
        ),
        data=orphan_payload(loose, conn),
        source=source,
    )
    return [f"🧟 orphans {level}: {res['headline']}"] if res["announced"] else []
