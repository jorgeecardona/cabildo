"""The numbers of the workspace's mode line, as data: ``cabildo stats --json``.

Other clients show them (the VS Code side bar). ``collect`` reads the sources the mode line
reads. A source with nothing to show leaves its group out.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from . import config, gateway, jupyter, store, usage
from .workspace import intents, plan, sessions, spend, upstreams
from .workspace import usage as cpu

Level = Literal["", "warn", "alert"]

WARN_PERCENT = 70.0
ALERT_PERCENT = 90.0
#: Two CPU samples this far apart give a session's CPU use.
SAMPLE_SECONDS = 0.5
FAILED = ("failed", "rejected", "expired")


@dataclass(frozen=True)
class Row:
    """One line: a name, its numbers, and more about it."""

    label: str
    value: str
    level: Level = ""
    detail: str = ""


@dataclass(frozen=True)
class Group:
    """The rows of one source. ``key`` stays the same; ``title`` is what a person reads."""

    key: str
    title: str
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class Agent:
    """One live session on the bus."""

    session_id: str
    name: str
    pid: int | None


def live_agents() -> tuple[Agent, ...]:
    """The active sessions of the bus, without the daemon and the person."""
    if not store.db_path().exists():
        return ()
    with contextlib.closing(store.connect()) as conn:
        return tuple(
            Agent(r["session_id"], store.display_name(r, r["session_id"]), r["claude_pid"])
            for r in store.active_sessions(conn)
            if r["session_id"] not in ("daemon", store.HUMAN_SESSION)
        )


def percent_level(percent: float) -> Level:
    """``warn`` from 70%, ``alert`` from 90%, as the mode line colours them."""
    return "alert" if percent >= ALERT_PERCENT else "warn" if percent >= WARN_PERCENT else ""


def _use_text(use: cpu.Use) -> str:
    return f"cpu {cpu.fmt_cpu(use.cpu)} · {cpu.fmt_mb(use.mem_mb)}"


def measure(pids: set[int]) -> dict[int, cpu.Use]:
    """Each process tree's CPU and memory. Empty where there is no ``/proc`` (macOS)."""
    if not pids or not cpu.available():
        return {}
    roots = dict.fromkeys(pids, True)
    first = cpu.sample_all(roots, cpu.EMPTY)
    time.sleep(SAMPLE_SECONDS)
    return cpu.sample_all(roots, first).uses


def _recorded(session_ids: tuple[str, ...]) -> dict[str, usage.Window]:
    """Each session's tokens and cost, from the usage table after reading the new lines."""
    if not session_ids or not store.db_path().exists():
        return {}
    with contextlib.closing(store.connect()) as conn:
        with contextlib.suppress(sqlite3.Error):  # another writer: use what is recorded
            usage.ingest(conn)
        try:
            return {i: usage.window(conn, 0.0, (i,)) for i in session_ids}
        except sqlite3.Error:
            return {}


def _last_reply(session_id: str) -> sessions.Tokens | None:
    found = list((sessions.config_dir() / "projects").glob(f"*/{session_id}.jsonl"))
    if not found:
        return None
    try:
        return sessions.last_reply(max(found, key=lambda p: p.stat().st_mtime))
    except OSError:
        return None


def session_row(
    agent: Agent,
    last: sessions.Tokens | None,
    total: usage.Window | None,
    use: cpu.Use | None,
    upstream: str,
    *,
    with_cost: bool,
) -> Row:
    """One session: model, how full its context is, CPU and memory; the rest in ``detail``."""
    parts: list[str] = []
    detail: list[str] = []
    level: Level = ""
    if last:
        if name := sessions.model_name(last.model):
            parts.append(name)
        used = cpu.fmt_count(last.context)
        if window := sessions.context_window(last.model):
            percent = 100 * last.context / window
            level = percent_level(percent)
            parts.append(f"ctx {percent:.0f}%")
            detail.append(f"context: {used} of {cpu.fmt_count(window)} ({percent:.0f}%)")
        else:
            parts.append(f"ctx {used}")
            detail.append(f"context: {used}")
        if last.effort:
            detail.append(f"effort: {last.effort}")
    if total and (total.output or total.input or total.cache_write):
        fresh = cpu.fmt_count(total.input + total.cache_write)
        detail.append(
            f"tokens: in {fresh} · out {cpu.fmt_count(total.output)} · "
            f"cache {cpu.fmt_count(total.cache_read)}"
        )
        if with_cost:
            detail.append(f"estimated cost: {cpu.fmt_money(total.cost)}")
    if upstream:
        detail.append(f"upstream: {upstream}")
    if use:
        parts.append(_use_text(use))
    return Row(agent.name, " · ".join(parts) or "no reply yet", level, "\n".join(detail))


def sessions_group(
    agents: tuple[Agent, ...],
    uses: Mapping[int, cpu.Use],
    view: upstreams.View | None,
    *,
    with_cost: bool,
) -> Group:
    """One row per live session."""
    last = {a.session_id: _last_reply(a.session_id) for a in agents}
    totals = _recorded(tuple(i for i, found in last.items() if found))
    rows: list[Row] = []
    for a in agents:
        serving = view.serving((a.session_id,)) if view else None
        rows.append(
            session_row(
                a,
                last[a.session_id],
                totals.get(a.session_id),
                uses.get(a.pid) if a.pid else None,
                serving.name if serving else "",
                with_cost=with_cost,
            )
        )
    return Group("sessions", "sessions", tuple(rows))


def plan_group(found: plan.Shared) -> Group:
    """The 5-hour and the 7-day limit: used, pace, reset, and when it fills."""
    rows: list[Row] = []
    for label, lim, pace, day in (
        ("5 hours", found.usage.five_hour, found.pace.five_hour, ""),
        ("7 days", found.usage.seven_day, found.pace.seven_day, "%a "),
    ):
        if not lim:
            continue
        parts = [f"{lim.percent:.0f}% used"]
        used_up = lim.percent >= 100
        if pace and pace.ready and not used_up:
            parts.append(f"{pace.rate:.1f}%/h")
        if lim.resets_at:
            parts.append(f"resets {time.strftime(day + '%H:%M', time.localtime(lim.resets_at))}")
        if pace and not used_up and (full := pace.full_at()):
            parts.append(f"full {time.strftime(day + '%H:%M', time.localtime(full))}")
        rows.append(Row(label, " · ".join(parts), percent_level(lim.percent)))
    return Group("plan", f"{found.usage.plan} plan".strip(), tuple(rows))


def gateway_group(view: upstreams.View) -> Group:
    """One row per upstream, in the order of the config file, with what it served."""
    rows: list[Row] = []
    for u in view.upstreams:
        state = "ready" if u.ready else f"set aside {cpu.fmt_age(u.set_aside_s)}"
        if u.forced:
            state += ", first"
        value = f"{u.requests} requests · {u.failovers} failovers · {state}"
        detail = [] if u.ready else [u.reason]
        if used := view.used(u):
            value += f" · {used}"
            detail.append(used)
        rows.append(Row(u.name, value, "" if u.ready else "warn", "\n".join(detail)))
    return Group("gateway", "gateway", tuple(rows))


def _gateway() -> tuple[upstreams.View | None, Group | None]:
    """The gateway's upstreams. A token in 1Password is not read here: a client asks every few
    seconds, and ``op`` can wait for a person.
    """
    table = config.gateway_table()
    if not table:
        return None, None
    try:
        settings = gateway.parse(table)
    except gateway.GatewayError:
        return None, None
    if settings.token and settings.token.scheme == "op":
        row = Row(
            "status",
            "not shown: the gateway token is in 1Password",
            detail="Run `cabildo gateway status` to see it.",
        )
        return None, Group("gateway", "gateway", (row,))
    view = upstreams.Reader().read()
    return view, gateway_group(view) if view else None


def spend_group(report: spend.Report, label: str) -> Group:
    """Estimated cost today, over 7 days and all time; per model in ``detail``."""
    rows: list[Row] = []
    for name, key, window, billed in (
        ("today", "today", report.spend.today, report.billed_today),
        ("7 days", "7d", report.spend.week, report.billed_week),
        ("all time", "all", report.spend.all_time, report.billed_all),
    ):
        value = cpu.fmt_money(window.cost)
        if billed is not None:
            value += f" · billed {cpu.fmt_money(billed)}"
        if window.unpriced:
            value += f" · {cpu.fmt_count(window.unpriced)} tokens unpriced"
        models = sorted(report.split.get(key, {}).values(), key=lambda m: -m.cost)
        detail = "\n".join(
            f"{sessions.model_name(m.model)}: {cpu.fmt_money(m.cost)}" for m in models if m.priced
        )
        rows.append(Row(name, value, detail=detail))
    return Group("spend", label, tuple(rows))


def intents_group(items: tuple[intents.Intent, ...], now: float) -> Group:
    """The push queue and what finished lately; a failure carries its reason."""
    rows = tuple(
        Row(
            f"{it.action} {it.project}",
            f"{it.status} · {cpu.fmt_age(now - it.at)}",
            "alert" if it.status in FAILED else "",
            "\n".join(x for x in (it.reason, f"asked by {it.by}") if x),
        )
        for it in items
    )
    return Group("intents", "intents", rows)


def runners_group(runners: tuple[jupyter.RunnerRow, ...], uses: Mapping[int, cpu.Use]) -> Group:
    """The Jupyter runners that run."""
    rows: list[Row] = []
    for r in runners:
        use = uses.get(r.pid) if r.pid else None
        value = " · ".join(
            x for x in ("paused" if r.paused else "running", _use_text(use) if use else "") if x
        )
        rows.append(Row(r.name, value, detail=r.cwd))
    return Group("runners", "jupyter runners", tuple(rows))


def _runners() -> tuple[jupyter.RunnerRow, ...]:
    try:
        return tuple(r for r in jupyter.statuses() if r.running)
    except (jupyter.RunnerError, OSError):
        return ()


def collect(cwd: str, now: float | None = None) -> tuple[Group, ...]:
    """Every group that has rows, in the order of the mode line.

    It reads transcripts, the bus DB and the gateway, and asks the plan usage at most once a
    minute (shared with the workspaces). ``cwd`` picks the ``[workspace].spend`` setting.
    """
    at = time.time() if now is None else now
    agents = live_agents()
    runners = _runners()
    uses = measure({p for p in (*(a.pid for a in agents), *(r.pid for r in runners)) if p})
    view, gateway_rows = _gateway()
    label = spend.heading(config.spend_mode(cwd))
    found: list[Group] = [sessions_group(agents, uses, view, with_cost=bool(label))]
    if shared := plan.shared(at - at % 60):
        found.append(plan_group(shared))
    if gateway_rows:
        found.append(gateway_rows)
    if label and (report := spend.load(at, cwd)):
        found.append(spend_group(report, label))
    found.append(intents_group(intents.load(at), at))
    found.append(runners_group(runners, uses))
    mine = {a.pid for a in agents if a.pid}
    if used := [u for pid, u in uses.items() if pid in mine]:
        total = Row("all sessions", _use_text(cpu.Use.total(used)))
        found.append(Group("machine", "cpu and memory", (total,)))
    return tuple(g for g in found if g.rows)


def text(groups: tuple[Group, ...]) -> str:
    """The groups as plain lines, for a terminal."""
    lines: list[str] = []
    for g in groups:
        lines.append(g.title)
        width = max(len(r.label) for r in g.rows)
        lines.extend(f"  {r.label:<{width}}  {r.value}" for r in g.rows)
    return "\n".join(lines) if lines else "nothing to show"
