"""``cabildo panel``: the workspace's list and its readings for another client, as JSON lines.

The VS Code side bar runs it. It prints one ``frame`` per line each time something it shows
changes, and reads one command per line on stdin. It ends when stdin closes.

A frame is data, not text lines: the client decides how to draw it. Amounts are sent as the
workspace writes them (``87k``, ``$0.32``, ``3m``), and as numbers where the client draws a bar.

Commands, each a JSON object with ``cmd``:

- ``select`` (``id``): the conversation ``Frame.detail`` describes; ``""`` describes none.
- ``scope`` (``scope``): ``here``, ``subfolders`` or ``all``.
- ``visible`` (``visible``): false stops reading until true.
- ``open`` (``id``, ``force``): answered with one ``open`` line, see ``Opened``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Literal, TextIO

from cabildo import cloud, config, gateway, host, host_client, jupyter, names, store
from cabildo import usage as usage_tokens
from cabildo.workspace import (
    agents,
    intents,
    launch,
    plan,
    rooms,
    routing,
    sessions,
    spend,
    upstreams,
    usage,
)
from cabildo.workspace.agents import AgentState
from cabildo.workspace.modeline import INTENT_FAILED, Scope, fmt_window, short_path
from cabildo.workspace.sessions import Row
from cabildo.workspace.usage import Use, fmt_age, fmt_count, fmt_cpu, fmt_mb, fmt_money

CLAUDE = "claude"
MAX_ITEMS = 200  # older conversations that do not run are counted, not sent
MAX_INTENTS = 20
NEW = "host:"  # the id of a hosted session that has no transcript yet: ``host:<host id>``
SCOPES: tuple[Scope, ...] = ("here", "subfolders", "all")
AMBER, RED = 70.0, 90.0  # percent of a context window or a plan limit, like the mode line
WINDOW_SECONDS = {"5h": 5 * 3600.0, "7d": 7 * 86400.0}

Mark = Literal["", "waiting", "working", "alive"]
Level = Literal["", "amber", "red"]
IntentKind = Literal["queue", "done", "failed"]


@dataclass(frozen=True)
class Item:
    """One conversation of the list."""

    id: str  # its newest session id, or ``host:<id>`` before it has a transcript
    title: str
    dir: str  # its directory, ~-relative
    branch: str = ""
    age: str = ""
    mark: Mark = ""
    group: str = ""
    tags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    agent: str = ""  # its cabildo name, "" when it was never on the bus
    rooms: tuple[str, ...] = ()  # ``#general``, ``#lan(shared)``
    cpu: str = ""
    mem: str = ""
    host_id: str = ""  # the session of the session host that runs it
    bus_id: str = ""  # the active bus session to send a message to


@dataclass(frozen=True)
class Context:
    """What the context window of the selected conversation holds."""

    used: str
    window: str = ""  # "" when the model's window is not known
    percent: float = 0.0  # of the window; 0 when it is not known
    level: Level = ""


@dataclass(frozen=True)
class Detail:
    """The selected conversation: what the list's row does not say. "" for a value not known."""

    agent: str = ""  # its robot name, also before it joined the bus
    model: str = ""
    context: Context | None = None  # None before the first reply
    effort: str = ""
    upstream: str = ""  # the gateway upstream that last served it
    tokens_in: str = ""  # without the cache reads
    tokens_out: str = ""
    tokens_cache: str = ""
    cost: str = ""  # of its whole life, at the rates of ``cabildo.prices``
    cpu: str = ""  # ``…`` until a second sample
    mem: str = ""


@dataclass(frozen=True)
class Limit:
    """One limit of the subscription."""

    name: str  # ``5h`` or ``7d``
    percent: float  # used
    level: Level = ""
    elapsed: float = -1.0  # percent of the window's time that passed; -1 when not known
    pace: str = ""  # ``5.9%/h``; "" when not measured yet, or nothing is left
    resets: str = ""  # local time; a 7-day one carries its weekday
    full: str = ""  # when it reaches 100% at this pace; "" when that is after the reset


@dataclass(frozen=True)
class Plan:
    """The subscription's limits."""

    name: str  # ``max``, ``pro``
    limits: tuple[Limit, ...] = ()
    elsewhere: bool = False  # another upstream serves the selected conversation


@dataclass(frozen=True)
class Upstream:
    """One upstream of the gateway. The counts are of all sessions since the daemon started."""

    name: str
    ready: bool
    requests: int = 0
    failovers: int = 0
    set_aside: str = ""  # for how long, when not ready
    reason: str = ""  # why it is set aside
    forced: bool = False  # tried first
    serving: bool = False  # it last served the selected conversation
    today: str = ""  # its cost, or tokens for a subscription; "" when it never served
    week: str = ""


@dataclass(frozen=True)
class Period:
    """One column of the spend table."""

    name: str  # ``today``, ``7d``, ``all``, ``session``
    total: str  # this machine's, from the transcripts; ``-`` when none
    billed: str = ""  # the organization's, from its cost API; ``-`` when not known
    unpriced: str = ""  # tokens the total leaves out


@dataclass(frozen=True)
class Model:
    """One model's row of the spend table."""

    name: str
    costs: tuple[str, ...] = ()  # one per period, ``-`` when none
    share: float = -1.0  # percent of the 7-day priced cost; -1 when none
    tokens: str = ""  # all time; never summed across models


@dataclass(frozen=True)
class Spend:
    """What the transcripts say was spent."""

    label: str  # ``api key`` or ``spend``
    periods: tuple[Period, ...] = ()
    models: tuple[Model, ...] = ()
    billed: bool = False  # the periods carry a billed amount


@dataclass(frozen=True)
class Intent:
    """One request to the daemon (a push), in the queue or finished lately."""

    action: str
    project: str
    status: str
    kind: IntentKind
    age: str
    by: str = ""
    reason: str = ""  # why it failed


@dataclass(frozen=True)
class Runner:
    """One running Jupyter runner, with its kernels."""

    name: str
    paused: bool = False
    cpu: str = ""  # ``…`` until measured
    mem: str = ""


@dataclass(frozen=True)
class Machine:
    """Everything the panel measures on this machine, together."""

    cpu: str
    mem: str


@dataclass(frozen=True)
class Frame:
    """Everything the client shows."""

    scope: Scope
    folder: str  # the directory the scopes are about, ~-relative
    items: tuple[Item, ...] = ()
    older: int = 0  # conversations left out
    selected: str = ""
    detail: Detail | None = None  # None when no conversation is selected
    plan: Plan | None = None
    gateway: tuple[Upstream, ...] = ()
    spend: Spend | None = None
    intents: tuple[Intent, ...] | None = None  # None: this machine never had one
    runners: tuple[Runner, ...] = ()
    machine: Machine | None = None  # None where CPU and memory cannot be read
    reading: bool = True  # the first read of the transcripts is not done
    type: str = "frame"


@dataclass(frozen=True)
class Opened:
    """The answer to ``open``: what a terminal runs.

    ``host_id`` set: attach to that session of the session host. ``command`` set: run it in
    ``cwd``. ``elsewhere``: another terminal runs the conversation; nothing was started, and
    ``force`` starts a second Claude on it. ``error``: why it cannot be opened.
    """

    id: str
    host_id: str = ""
    command: tuple[str, ...] = ()
    cwd: str = ""
    title: str = ""
    elsewhere: bool = False
    error: str = ""
    type: str = "open"


@dataclass(frozen=True)
class Readings:
    """What the sources last said. ``None`` for a source not read yet."""

    rows: tuple[Row, ...] | None = None
    live: Mapping[str, AgentState] | None = None
    hosted: tuple[host.Record, ...] = ()
    bus: frozenset[str] = frozenset()
    sample: usage.Round = usage.EMPTY
    plan: plan.Shared | None = None
    upstreams: upstreams.View | None = None
    spend: spend.Report | None = None
    spend_label: str = ""
    spend_ids: tuple[str, ...] = ()  # the conversation ``spend.session`` is about
    intents: tuple[intents.Intent, ...] | None = None
    runners: tuple[jupyter.RunnerRow, ...] = ()


@dataclass(frozen=True)
class View:
    """What the client asked to see."""

    cwd: str
    scope: Scope = "here"
    selected: str = ""


def _live_of(ids: Iterable[str], live: Mapping[str, AgentState]) -> AgentState | None:
    """The newest session of a conversation that ``claude agents`` reports."""
    return next((st for sid in reversed(tuple(ids)) if (st := live.get(sid))), None)


def _host_of(row: Row, readings: Readings) -> host.Record | None:
    """The hosted session that runs ``row``: tagged with one of its ids, or running its pid."""
    live = readings.live or {}
    pids = {st.pid for sid in row.ids if (st := live.get(sid)) and st.pid}
    return next(
        (h for h in readings.hosted if h.session_id in row.ids or h.pid in pids),
        None,
    )


def _pids(row: Row, readings: Readings) -> set[int]:
    live = readings.live or {}
    pids = {st.pid for sid in row.ids if (st := live.get(sid)) and st.pid}
    owned = _host_of(row, readings)
    return pids | {owned.pid} if owned else pids


def _mark(state: AgentState | None, hosted: bool) -> Mark:
    if state and state.needs_input:
        return "waiting"
    if state and state.working:
        return "working"
    return "alive" if hosted or (state and state.alive) else ""


def _use(pids: set[int], uses: Mapping[int, Use]) -> Use | None:
    found = [uses[p] for p in pids if p in uses]
    return Use.total(found) if found else None


def _in_scope(path: str, view: View) -> bool:
    if view.scope == "all":
        return True
    bases = {view.cwd, os.path.realpath(view.cwd)}
    if view.scope == "here":
        return path in bases
    return any(sessions.under(path, b) for b in bases)


def _level(percent: float) -> Level:
    return "red" if percent >= RED else "amber" if percent >= AMBER else ""


def _item(row: Row, readings: Readings) -> Item:
    state = _live_of(row.ids, readings.live or {})
    owned = _host_of(row, readings)
    use = _use(_pids(row, readings), readings.sample.uses)
    return Item(
        id=row.head.session_id,
        title=row.display_title,
        dir=short_path(row.cwd, 200),
        branch=row.git_branch if row.git_branch != "HEAD" else "",
        age=row.age,
        mark=_mark(state, owned is not None),
        group=row.meta.group,
        tags=row.meta.tags,
        notes=tuple(n.text for n in row.notes),
        agent=row.rooms.agent,
        rooms=tuple(rooms.label(replace(row.rooms, topics=(t,))) for t in row.rooms.topics),
        cpu=fmt_cpu(use.cpu) if use else "",
        mem=fmt_mb(use.mem_mb) if use else "",
        host_id=owned.id if owned else "",
        bus_id=next((i for i in reversed(row.ids) if i in readings.bus), ""),
    )


def _new_items(view: View, readings: Readings, now: float) -> list[Item]:
    """The hosted sessions of this scope that no conversation of the list stands for."""
    rows = readings.rows or ()
    taken = {h.id for r in rows if (h := _host_of(r, readings))}
    live = readings.live or {}
    found: list[Item] = []
    for h in sorted(readings.hosted, key=lambda h: -h.started_at):
        if h.id in taken or not _in_scope(h.cwd, view):
            continue
        state = next((st for st in live.values() if st.pid == h.pid), None)
        use = _use({h.pid}, readings.sample.uses)
        sid = state.session_id if state else h.session_id
        found.append(
            Item(
                id=NEW + h.id,
                title=(state.name if state else "") or "new conversation",
                dir=short_path(h.cwd, 200),
                age=fmt_age(max(0.0, now - h.started_at)),
                mark=_mark(state, hosted=True),
                agent=names.robot_name(sid) if sid else "",
                cpu=fmt_cpu(use.cpu) if use else "",
                mem=fmt_mb(use.mem_mb) if use else "",
                host_id=h.id,
                bus_id=sid if sid in readings.bus else "",
            )
        )
    return found


def _capped(rows: tuple[Row, ...], readings: Readings) -> tuple[list[Row], int]:
    """At most ``MAX_ITEMS`` rows, in their order: every running one, then the newest."""
    if len(rows) <= MAX_ITEMS:
        return list(rows), 0
    newest = sorted(rows, key=lambda r: (not _pids(r, readings), -r.mtime))[:MAX_ITEMS]
    kept = {r.head.session_id for r in newest}
    return [r for r in rows if r.head.session_id in kept], len(rows) - len(kept)


def _serving(row: Row | None, readings: Readings) -> gateway.UpstreamStatus | None:
    view = readings.upstreams
    return view.serving(row.ids) if view and row else None


def _context(t: sessions.Tokens) -> Context | None:
    if not (t.context or t.output):
        return None
    window = sessions.context_window(t.model)
    if not window:
        return Context(fmt_count(t.context))
    percent = 100 * t.context / window
    return Context(fmt_count(t.context), fmt_window(window), round(percent, 1), _level(percent))


def _usage_of(pids: set[int], readings: Readings) -> tuple[str, str]:
    """CPU and memory of ``pids``; CPU is ``…`` after one sample, both "" when none runs."""
    use = _use(pids, readings.sample.uses)
    if use:
        return fmt_cpu(use.cpu), fmt_mb(use.mem_mb)
    first = [s for p in pids if (s := readings.sample.samples.get(p))]
    if first:
        return "…", fmt_mb(sum(p.anon_kb for s in first for p in s.procs) / 1024)
    return "", ""


def _detail(row: Row | None, new: Item | None, readings: Readings) -> Detail | None:
    if new:
        cpu, mem = _usage_of({h.pid for h in readings.hosted if h.id == new.host_id}, readings)
        return Detail(agent=new.agent, cpu=cpu, mem=mem)
    if row is None:
        return None
    t = row.tokens
    replied = bool(t.context or t.output)
    serving = _serving(row, readings)
    mine = readings.spend.session if readings.spend and readings.spend_ids == row.ids else None
    cpu, mem = _usage_of(_pids(row, readings), readings)
    return Detail(
        agent=names.robot_name(row.head.session_id),
        model=sessions.model_name(t.model) if replied else "",
        context=_context(t),
        effort=t.effort if replied else "",
        upstream=serving.name if serving else "",
        tokens_in=fmt_count(t.input - t.cached) if replied else "",
        tokens_out=fmt_count(t.output) if replied else "",
        tokens_cache=fmt_count(t.cached) if replied and t.cached else "",
        cost=fmt_money(mine.all_time.cost) if mine and readings.spend_label else "",
        cpu=cpu,
        mem=mem,
    )


def _limit(name: str, lim: plan.Limit, pace: plan.Pace | None, now: float) -> Limit:
    day = "%a " if name == "7d" else ""
    used_up = lim.percent >= 100  # nothing left to fill: no pace, no time it fills
    full = pace.full_at() if pace and not used_up else None
    elapsed = -1.0
    if lim.resets_at:
        start = lim.resets_at - WINDOW_SECONDS[name]
        elapsed = round(min(100.0, max(0.0, 100 * (now - start) / WINDOW_SECONDS[name])), 1)
    return Limit(
        name=name,
        percent=round(lim.percent, 1),
        level=_level(lim.percent),
        elapsed=elapsed,
        pace=f"{pace.rate:.1f}%/h" if pace and pace.ready and not used_up else "",
        resets=time.strftime(f"{day}%H:%M", time.localtime(lim.resets_at)) if lim.resets_at else "",
        full=time.strftime(f"{day}%H:%M", time.localtime(full)) if full else "",
    )


def _plan(row: Row | None, readings: Readings, now: float) -> Plan | None:
    if not readings.plan:
        return None
    found, pace = readings.plan.usage, readings.plan.pace
    serving = _serving(row, readings)
    limits = (
        ("5h", found.five_hour, pace.five_hour),
        ("7d", found.seven_day, pace.seven_day),
    )
    return Plan(
        name=found.plan,
        limits=tuple(_limit(name, lim, at, now) for name, lim, at in limits if lim),
        elsewhere=serving is not None and serving.kind != "passthrough",
    )


def _gateway(row: Row | None, readings: Readings) -> tuple[Upstream, ...]:
    view = readings.upstreams
    if not view:
        return ()
    serving = _serving(row, readings)
    spent = {s.upstream: s for s in view.spent}
    found: list[Upstream] = []
    for u in view.upstreams:
        used, priced = spent.get(u.name), u.kind != "passthrough"
        found.append(
            Upstream(
                name=u.name,
                ready=u.ready,
                requests=u.requests,
                failovers=u.failovers,
                set_aside="" if u.ready else fmt_age(u.set_aside_s),
                reason="" if u.ready else u.reason,
                forced=u.forced,
                serving=serving is not None and serving.name == u.name,
                today=upstreams.spent_value(used.today, priced=priced) if used else "",
                week=upstreams.spent_value(used.week, priced=priced) if used else "",
            )
        )
    return tuple(found)


def _spend(row: Row | None, readings: Readings) -> Spend | None:
    r = readings.spend
    if not (r and readings.spend_label):
        return None
    windows: list[tuple[str, usage_tokens.Window, float | None]] = [
        ("today", r.spend.today, r.billed_today),
        ("7d", r.spend.week, r.billed_week),
        ("all", r.spend.all_time, r.billed_all),
    ]
    if r.session is not None and row is not None and readings.spend_ids == row.ids:
        windows.append(("session", r.session.all_time, None))
    week = r.split.get("7d", {})
    spent = sum(m.cost for m in week.values() if m.priced)

    def cost(period: str, model: str) -> str:
        found = r.split.get(period, {}).get(model)
        return fmt_money(found.cost) if found and found.priced else "-"

    def share(model: str) -> float:
        found = week.get(model)
        return round(100 * found.cost / spent, 1) if spent and found and found.priced else -1.0

    return Spend(
        label=readings.spend_label,
        periods=tuple(
            Period(
                name=name,
                total=fmt_money(window.cost),
                billed=("-" if billed is None else fmt_money(billed))
                if r.any_billed and name != "session"
                else "",
                unpriced=fmt_count(window.unpriced) if window.unpriced else "",
            )
            for name, window, billed in windows
        ),
        models=tuple(
            Model(
                name=sessions.model_name(m.model),
                costs=tuple(cost(name, m.model) for name, *_ in windows),
                share=share(m.model),
                tokens=fmt_count(m.tokens),
            )
            for m in r.models
        ),
        billed=r.any_billed,
    )


def _intent(it: intents.Intent, now: float) -> Intent:
    failed = it.status in INTENT_FAILED
    return Intent(
        action=it.action,
        project=it.project,
        status=it.status,
        kind="failed" if failed else "queue" if it.waiting else "done",
        age=fmt_age(now - it.at),
        by=it.by,
        reason=it.reason if failed else "",
    )


def _runners(readings: Readings) -> tuple[Runner, ...]:
    found: list[Runner] = []
    for r in readings.runners:
        if not r.running:
            continue
        use = readings.sample.uses.get(r.pid) if r.pid else None
        cpu, mem = (fmt_cpu(use.cpu), fmt_mb(use.mem_mb)) if use else ("…", "…")
        found.append(Runner(r.name, r.paused, cpu, mem))
    return tuple(found)


def _machine(readings: Readings) -> Machine | None:
    if not usage.available():
        return None
    uses = readings.sample.uses
    if not uses:
        return Machine("-", "-")
    total = Use.total(list(uses.values()))
    return Machine(fmt_cpu(total.cpu), fmt_mb(total.mem_mb))


def frame(view: View, readings: Readings, now: float) -> Frame:
    """What the client shows for ``view``, from ``readings``."""
    rows, older = _capped(readings.rows or (), readings)
    new = _new_items(view, readings, now) if readings.rows is not None else []
    row = next((r for r in rows if view.selected in r.ids), None)
    chosen = next((i for i in new if i.id == view.selected), None)
    queued = readings.intents
    return Frame(
        scope=view.scope,
        folder=short_path(view.cwd, 200),
        items=(*new, *(_item(r, readings) for r in rows)),
        older=older,
        selected=row.head.session_id if row else chosen.id if chosen else "",
        detail=_detail(row, chosen, readings),
        plan=_plan(row, readings, now),
        gateway=_gateway(row, readings),
        spend=_spend(row, readings),
        intents=None if queued is None else tuple(_intent(i, now) for i in queued[:MAX_INTENTS]),
        runners=_runners(readings),
        machine=_machine(readings),
        reading=readings.rows is None,
    )


def open_conversation(view: View, readings: Readings, ident: str, force: bool) -> Opened:
    """Say what a terminal runs to show ``ident``, starting it in the session host if needed.

    It asks ``claude agents`` and the host again, so a conversation that started since the
    last read is not started twice.
    """
    if ident.startswith(NEW):
        return Opened(ident, host_id=ident.removeprefix(NEW))
    row = next((r for r in readings.rows or () if ident in r.ids), None)
    if row is None:
        return Opened(ident, error="this conversation is not in the list any more")
    fresh = agents.shared_live_agents(time.time() - 1).agents
    readings = replace(
        readings,
        live=fresh if fresh is not None else readings.live,
        hosted=tuple(host_client.list_sessions()),
    )
    title = row.display_title
    owned = _host_of(row, readings)
    if owned is not None:
        return Opened(ident, host_id=owned.id, title=title)
    state = _live_of(row.ids, readings.live or {})
    try:
        found = routing.route(
            row,
            view.cwd,
            (readings.live or {}).get(row.head.session_id),
            readings.live is not None,
            lambda: readings.hosted,
            launch.load(),
            CLAUDE,
        )
    except launch.ConfigError as exc:
        return Opened(ident, title=title, error=str(exc))
    if found is None:
        where = short_path(row.cwd, 60)
        return Opened(ident, title=title, error=f"its directory {where} is gone")
    cwd = found.cwd or view.cwd
    if found.kind == "attach":
        return Opened(ident, command=found.command, cwd=cwd, title=title)
    if state and state.alive and not force:
        return Opened(ident, title=title, elsewhere=True)
    try:
        record = host_client.start_session(
            found.command, cwd=cwd, group=row.meta.group, session_id=found.session_id
        )
    except (host_client.ClientError, OSError) as exc:
        return Opened(ident, title=title, error=f"session host: {exc}")
    return Opened(ident, host_id=record.id, title=title)


# --- the process -------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One thing to read every ``seconds``: ``read`` returns the changed ``Readings`` fields."""

    name: str
    seconds: float
    read: Callable[[View, Readings], dict[str, object]]


def _rows(view: View, _readings: Readings) -> dict[str, object]:
    cwd = None if view.scope == "all" else view.cwd
    found = sessions.build_rows(cwd, subfolders=view.scope == "subfolders")
    return {"rows": tuple(sessions.attach_notes(sessions.attach_rooms(found)))}


def _live(_view: View, _readings: Readings) -> dict[str, object]:
    found = agents.shared_live_agents(time.time() - 5).agents
    return {} if found is None else {"live": found}  # a failed call says nothing


def _hosted(_view: View, _readings: Readings) -> dict[str, object]:
    return {"hosted": tuple(host_client.list_sessions())}


def _bus(_view: View, _readings: Readings) -> dict[str, object]:
    if not store.db_path().exists():
        return {"bus": frozenset()}
    conn = store.connect()
    try:
        return {"bus": frozenset(r["session_id"] for r in store.active_sessions(conn))}
    finally:
        conn.close()


def _sample(_view: View, readings: Readings) -> dict[str, object]:
    if not usage.available():
        return {}
    live = readings.live or {}
    listed = {sid for r in readings.rows or () for sid in r.ids}
    pids = {st.pid for st in live.values() if st.pid and st.session_id in listed}
    pids |= {h.pid for h in readings.hosted}
    pids |= {r.pid for r in readings.runners if r.pid and r.running}
    return {"sample": usage.sample_all(dict.fromkeys(pids, True), readings.sample)}


def _read_plan(_view: View, _readings: Readings) -> dict[str, object]:
    now = time.time()
    found = plan.shared(now - now % 60)  # this minute's sample, shared with the workspaces
    return {"plan": found} if found else {}


class _Upstreams:
    """The gateway's status. It keeps the reader, so a 1Password token is read only once."""

    def __init__(self) -> None:
        self._reader = upstreams.Reader()

    def __call__(self, _view: View, _readings: Readings) -> dict[str, object]:
        table = config.gateway_table()
        if table:
            try:
                token = gateway.parse(table).token
            except gateway.GatewayError:
                return {"upstreams": None}
            if token and token.scheme == "op":  # asking would open 1Password's prompt
                return {"upstreams": None}
        return {"upstreams": self._reader.read()}


def _read_spend(view: View, readings: Readings) -> dict[str, object]:
    label = spend.heading(config.spend_mode(view.cwd))
    if not label:
        return {"spend": None, "spend_label": "", "spend_ids": ()}
    row = next((r for r in readings.rows or () if view.selected in r.ids), None)
    ids = row.ids if row else ()
    found = spend.load(time.time(), view.cwd, ids)
    return {"spend": found, "spend_label": label, "spend_ids": ids} if found else {}


def _intents(view: View, readings: Readings) -> dict[str, object]:
    found = intents.load(time.time())
    # no group on a machine that never had an intent; once shown it stays
    if not found and readings.intents is None:
        return {}
    key = cloud.project_key(view.cwd)
    return {"intents": intents.in_scope(found, view.scope, view.cwd, key)}


def _read_runners(_view: View, _readings: Readings) -> dict[str, object]:
    return {"runners": tuple(jupyter.statuses())}


def sources() -> tuple[Source, ...]:
    """Every source, in the order ``collect`` reads them: later ones use earlier answers."""
    return (
        Source("rows", 3, _rows),
        Source("live", 10, _live),
        Source("hosted", 3, _hosted),
        Source("bus", 5, _bus),
        Source("runners", 3, _read_runners),
        Source("sample", 5, _sample),
        Source("intents", 3, _intents),
        Source("upstreams", 5, _Upstreams()),
        Source("plan", 15, _read_plan),
        Source("spend", 60, _read_spend),
    )


def _read(source: Source, view: View, readings: Readings) -> dict[str, object]:
    """One read; a source that fails says nothing, so its last answer stays."""
    try:
        return source.read(view, readings)
    except Exception as exc:  # one broken source must not end the panel
        print(f"panel: {source.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {}


def collect(view: View) -> Frame:
    """One frame with every source read once, for ``--once``. CPU needs two samples."""
    readings = Readings()
    for source in sources():
        readings = replace(readings, **_read(source, view, readings))
        if source.name == "sample":
            time.sleep(0.5)
            readings = replace(readings, **_read(source, view, readings))
    return frame(view, readings, time.time())


@dataclass
class _State:
    view: View
    readings: Readings = field(default_factory=Readings)
    generation: int = 0  # grows when the scope changes: older answers are dropped
    visible: bool = True
    closed: bool = False


class Server:
    """Reads the sources on one thread each and writes a frame when it changes."""

    def __init__(self, view: View, out: TextIO, every: tuple[Source, ...] | None = None) -> None:
        self._state = _State(view)
        self._out = out
        self._sources = sources() if every is None else every
        self._lock = threading.Lock()  # the state
        self._writing = threading.Lock()
        self._changed = threading.Event()
        self._wake = {s.name: threading.Event() for s in self._sources}
        self._last = ""

    def _send(self, message: Frame | Opened) -> None:
        line = json.dumps(asdict(message), ensure_ascii=False)
        with self._writing:
            self._out.write(line + "\n")
            self._out.flush()

    def _wake_up(self, *wanted: str) -> None:
        for name, event in self._wake.items():
            if not wanted or name in wanted:
                event.set()

    def _poll(self, source: Source) -> None:
        wake = self._wake[source.name]
        while True:
            with self._lock:
                state = self._state
                view, readings = state.view, state.readings
                generation, visible = state.generation, state.visible
                if state.closed:
                    return
            if visible:
                found = _read(source, view, readings)
                with self._lock:
                    if found and generation == self._state.generation:
                        had_rows = self._state.readings.rows is not None
                        self._state.readings = replace(self._state.readings, **found)
                        self._changed.set()
                        if source.name == "rows" and not had_rows:
                            self._wake_up("sample", "spend")  # they were waiting for the rows
            quick = source.name == "sample" and not self._state.readings.sample.uses
            wake.wait(0.5 if quick else source.seconds)
            wake.clear()

    def _write_frames(self) -> None:
        while True:
            self._changed.wait(1.0)  # at least once a second: ages move on their own
            self._changed.clear()
            with self._lock:
                if self._state.closed:
                    return
                if not self._state.visible:
                    continue
                view, readings = self._state.view, self._state.readings
            found = frame(view, readings, time.time())
            line = json.dumps(asdict(found), ensure_ascii=False)
            if line != self._last:
                self._last = line
                try:
                    self._send(found)
                except (BrokenPipeError, ValueError):
                    return

    def command(self, line: str) -> None:
        """Apply one line of stdin; a line that is not a known command is ignored."""
        try:
            raw = json.loads(line)
            name = raw["cmd"]
        except (ValueError, KeyError, TypeError):
            return
        if name == "open":
            with self._lock:
                view, readings = self._state.view, self._state.readings
            ident, force = str(raw.get("id", "")), raw.get("force") is True
            threading.Thread(
                target=lambda: self._send(open_conversation(view, readings, ident, force)),
                daemon=True,
            ).start()
            return
        wake: tuple[str, ...] = ()
        with self._lock:
            state = self._state
            if name == "select":
                state.view = replace(state.view, selected=str(raw.get("id", "")))
                wake = ("spend",)
            elif name == "scope" and raw.get("scope") in SCOPES:
                state.view = replace(state.view, scope=raw["scope"], selected="")
                state.readings = replace(state.readings, rows=None, intents=None)
                state.generation += 1
                wake = ("rows", "intents")
            elif name == "visible":
                state.visible = raw.get("visible") is True
                if state.visible:
                    self._last = ""  # the client may have dropped what it showed
                    wake = tuple(self._wake)
            else:
                return
        self._changed.set()
        if wake:
            self._wake_up(*wake)

    def serve(self, commands: Iterable[str]) -> None:
        """Run until ``commands`` ends."""
        threads = [
            threading.Thread(target=self._poll, args=(s,), daemon=True) for s in self._sources
        ]
        writer = threading.Thread(target=self._write_frames, daemon=True)
        for t in (*threads, writer):
            t.start()
        try:
            for line in commands:
                self.command(line)
        finally:
            with self._lock:
                self._state.closed = True
            self._changed.set()
            self._wake_up()
            writer.join(timeout=5)


def run(cwd: str, scope: Scope, once: bool) -> int:
    """The ``cabildo panel`` command."""
    view = View(cwd, scope)
    if once:
        print(json.dumps(asdict(collect(view)), ensure_ascii=False))
        return 0
    Server(view, sys.stdout).serve(sys.stdin)
    return 0
