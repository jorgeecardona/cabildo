"""The lines of the mode line, as text. The workspace shows them under its list, and
``cabildo panel`` sends the same lines to other clients (the VS Code side bar).

``compose`` returns groups of lines; each line is a (text, style) pair with a Rich style such as
``"dim"`` or ``"bold red"``. A client draws a rule between two groups. Nothing here imports
Textual.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from cabildo import gateway, jupyter
from cabildo import usage as usage_tokens
from cabildo.workspace import intents, plan, sessions, spend, upstreams
from cabildo.workspace.plan import PlanUsage
from cabildo.workspace.sessions import Row, Tokens
from cabildo.workspace.usage import Use, fmt_age, fmt_count, fmt_cpu, fmt_mb, fmt_money


def short_path(path: str, width: int) -> str:
    """~-relative, cut from the left so the end of the path stays visible."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + "/"):
        path = "~" + path[len(home) :]
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1) :] if width > 1 else "…"[: max(width, 0)]


def fmt_window(n: int) -> str:
    """``1M``, ``200k``: a window size without a decimal."""
    return f"{n // 1_000_000}M" if n % 1_000_000 == 0 else fmt_count(n)


def grid(label: str, a: str, b: str, tail: str = "") -> str:
    """One mode-line row: a label, then two right-aligned columns, so the numbers of the
    context, tokens, and CPU and memory rows line up under each other.
    """
    return f"{label:<11}{a:>10}{b:>10}{tail}"


def fmt_context(t: Tokens) -> str:
    """``opus 5.5     ctx 299k    of 1M  30%``; empty before the first reply.
    The window is left out for a model whose window is not known.
    """
    if not (t.context or t.output):
        return ""
    window = sessions.context_window(t.model)
    name = sessions.model_name(t.model) or "context"  # "" when the line had no model
    used = f"ctx {fmt_count(t.context)}"
    if not window:
        return grid(name, used, "")
    return grid(name, used, f"of {fmt_window(window)}", f"{100 * t.context / window:>4.0f}%")


def context_style(t: Tokens) -> str:
    """The context line's style once the window is nearly full: amber from 70%, red from
    90%; empty below, or when the window is not known.
    """
    window = sessions.context_window(t.model)
    if not window:
        return ""
    full = t.context / window
    return "bold red" if full >= 0.9 else "bold dark_orange3" if full >= 0.7 else ""


def fmt_tokens(t: Tokens, width: int = 1000) -> str:
    """``tokens       in 1.2M  out 87k  cache 32.4M``: summed over every reply; empty
    before the first. ``in`` leaves out the cache reads, shown apart when ``width`` has
    room for them.
    """
    if not (t.context or t.output):
        return ""
    fresh, out = f"in {fmt_count(t.input - t.cached)}", f"out {fmt_count(t.output)}"
    line = grid("tokens", fresh, out, f"  cache {fmt_count(t.cached)}" if t.cached else "")
    return line if len(line) <= width else grid("tokens", fresh, out)


def fmt_effort(t: Tokens) -> str:
    """``effort       high``: the reasoning effort of the last reply; empty when not known."""
    return grid("effort", t.effort, "") if t.effort else ""


def fmt_use(label: str, use: Use | None, first_mb: float | None = None) -> str:
    """One row of the mode line's CPU and memory table; ``-`` when nothing runs. With only
    a first sample (``first_mb``), CPU is ``…``: it needs two samples, memory needs one.
    """
    if use:
        return grid(label, fmt_cpu(use.cpu), fmt_mb(use.mem_mb))
    if first_mb is not None:
        return grid(label, "…", fmt_mb(first_mb))
    return grid(label, "-", "-")


USE_HEADER = grid("", "cpu", "mem")


Scope = Literal["here", "subfolders", "all"]


SCOPE_NAMES = {"here": "{proj}", "subfolders": "{proj}/…", "all": "all directories"}


def fmt_plan(p: PlanUsage, pace: plan.PlanPace | None = None) -> list[str]:
    """The plan table, one row per limit, under a header::

        max plan        used    pace  resets  ETA
        5h               23%  5.9%/h  18:59
        7d               53%  0.9%/h  Sat     Thu 00:46

    ``pace`` is percent per hour, smoothed; ``ETA`` is when the limit reaches 100% at
    that pace, shown only when that is before the reset. A limit at 100% shows neither.
    Times are local; a 7-day time carries its weekday.
    """
    rows = [grid(f"{p.plan} plan".strip(), "used", "pace", f"  {'resets':<8}ETA")]
    for name, lim, at in (
        ("5h", p.five_hour, pace.five_hour if pace else None),
        ("7d", p.seven_day, pace.seven_day if pace else None),
    ):
        if not lim:
            continue
        day = "%a" if name == "7d" else ""
        reset = (
            time.strftime(day or "%H:%M", time.localtime(lim.resets_at)) if lim.resets_at else ""
        )
        used_up = lim.percent >= 100  # nothing left to fill: no pace, no ETA
        full = at.full_at() if at and not used_up else None
        when = time.strftime(f"{day} %H:%M".strip(), time.localtime(full)) if full else ""
        rate = (f"{at.rate:.1f}%/h" if at.ready else "-") if at and not used_up else ""
        rows.append(grid(name, f"{lim.percent:.0f}%", rate, f"  {reset:<8}{when}").rstrip())
    return rows


def fmt_upstream(up: gateway.UpstreamStatus) -> str:
    """``upstream  bedrock-eu``: which gateway upstream last served the shown conversation."""
    return grid("upstream", up.name[:20], "").rstrip()


def fmt_gateway(view: upstreams.View, serving: str = "") -> list[tuple[str, str]]:
    """The gateway table, one row per upstream in the order of the config file, as (line,
    style) pairs::

        gateway      requests failovers
        anthropic         412        37  set aside 3d
          rate or usage limit
        bedrock            38         0  ready

    The counts are of all sessions since the daemon started. ``serving`` names the upstream
    of the shown conversation; its row is bold. An upstream that served a reply has a row with
    its cost (or tokens) of today and of 7 days.
    """
    out: list[tuple[str, str]] = [(grid("gateway", "requests", "failovers"), "")]
    for u in view.upstreams:
        state = "ready" if u.ready else f"set aside {fmt_age(u.set_aside_s)}"
        if u.forced:
            state += ", first"
        line = grid(u.name[:10], str(u.requests), str(u.failovers), f"  {state}")
        out.append((line, "bold" if u.name == serving else ""))
        if used := view.used(u):
            out.append((f"  {used}", "dim"))
        if not u.ready and u.reason:
            out.append((f"  {u.reason}", "dim"))
    return out


INTENT_LINES = (
    7  # the intents group never takes more rows than this: the screen height is the limit
)


RUNNER_LINES = 4  # the runners group never takes more rows than this


INTENT_FAILED = ("failed", "rejected", "expired")


def fmt_intents(items: tuple[intents.Intent, ...], now: float) -> list[tuple[str, str]]:
    """The intents group, as (line, style) pairs::

        intents   state   age
        push cabildo   running    2m
        push timbre    pending    1m
        push cabildo   failed    12m
          working tree has uncommitted tracked changes

    The queue first, then what finished lately. A failure shows its reason on the next row, in
    red. Beyond ``INTENT_LINES`` rows the rest is counted (``+2 more``).
    """
    if not items:
        return [("intents    none in the last hour", "")]
    out: list[tuple[str, str]] = [(grid("intents", "state", "age"), "")]
    shown = 0
    for it in items:
        failed = it.status in INTENT_FAILED
        rows = [
            (
                f"{it.action[:5]:<5}{it.project[:12]:<13}{it.status:<9}{fmt_age(now - it.at):>4}",
                "bold red" if failed else "",
            )
        ]
        if failed and it.reason:
            rows.append((f"  {it.reason}", "red"))
        if len(out) + len(rows) > INTENT_LINES + 1:
            break
        out.extend(rows)
        shown += 1
    if shown < len(items):
        out.append((f"+{len(items) - shown} more", "dim"))
    return out


def fmt_runners(
    runners: tuple[jupyter.RunnerRow, ...], uses: Mapping[int, Use]
) -> list[tuple[str, str]]:
    """The Jupyter runners group, one row each: a mark (``●`` running, ``‖`` paused), the name,
    then CPU and memory of the server with its kernels. Empty when no runner runs.
    """
    live = [r for r in runners if r.running]
    if not live:
        return []
    out: list[tuple[str, str]] = [(grid("runners", "cpu", "mem"), "")]
    for r in live[:RUNNER_LINES]:
        use = uses.get(r.pid) if r.pid else None
        mark = "‖" if r.paused else "●"
        cpu, mem = (fmt_cpu(use.cpu), fmt_mb(use.mem_mb)) if use else ("…", "…")
        out.append((grid(f"{mark} {r.name[:8]}", cpu, mem), "dim" if r.paused else ""))
    if len(live) > RUNNER_LINES:
        out.append((f"+{len(live) - RUNNER_LINES} more", "dim"))
    return out


COL = 10  # every numeric column, so the rows line up under each other


LABEL = 11


TOTAL, BILLED, TOKENS, SHARE = "total", "billed", "tokens", "7d share"


def fmt_spend(r: spend.Report, label: str = "api key", width: int = 1000) -> list[str]:
    """One matrix: a row per period, a column per model, and what each row came to::

        api key        opus 5 sonnet 5.5     total    billed
        today           $1.59      $0.03     $1.62         -
        7d          $4,980.11     $74.00 $5,054.11 $4,712.08
        all         $7,800.00    $170.69 $7,970.69 $21,004.55
        session       $310.25          -   $310.25
        tokens          623.8M     115.9M

    ``total`` is this machine's, computed from the transcripts at the rates in
    `cabildo.prices`. ``billed`` is the organization's, from its cost API -- a different
    question, so a different column: it covers every machine and lands hours late, which
    is why today's is usually a dash rather than a zero.

    ``session`` is the conversation in the main pane, over its whole life.

    ``7d share`` is each model's part of the last week's priced cost, in percent. It is the
    share of money, because a share of tokens would be mostly cache reads, which are cheap
    and not comparable across models.

    The ``tokens`` row has no total on purpose. Summing tokens across models is the one
    number this table refuses to print: a Haiku token and a Fable token are 80 times apart
    in price. Beside the name of the model that spent them they mean something, so that is
    the only place they appear.

    Narrow lists lose the cheapest model columns first; ``total`` and ``billed`` stay.
    """
    models = [m.model for m in r.models]

    periods: list[tuple[str, usage_tokens.Window | None, float | None]] = [
        ("today", r.spend.today, r.billed_today),
        ("7d", r.spend.week, r.billed_week),
        ("all", r.spend.all_time, r.billed_all),
    ]
    if r.session is not None:
        periods.append(("session", r.session.all_time, None))

    by_model = {m.model: m for m in r.models}

    def column_cells(column: str) -> list[str]:
        """The header and every row's cell for one column, top to bottom."""
        cells = [sessions.model_name(column) if column not in (TOTAL, BILLED) else column]
        for name, window, billed in periods:
            if column == TOTAL:
                cells.append(fmt_money(window.cost) if window else "-")
            elif column == BILLED:
                cells.append("-" if billed is None else fmt_money(billed))
            else:
                found = r.split.get(name, {}).get(column)
                cells.append(fmt_money(found.cost) if found and found.priced else "-")
        week = r.split.get("7d", {})
        spent = sum(m.cost for m in week.values() if m.priced)
        if column == TOTAL:
            cells.append("100%" if spent else "-")
        elif column == BILLED:
            cells.append("")
        else:
            found = week.get(column)
            cells.append(
                f"{100 * found.cost / spent:.0f}%" if spent and found and found.priced else "-"
            )
        # Tokens, per model, over the same window as the `all` row -- and no total. The
        # blank is the point: a sum of tokens across models is the number this refuses.
        found = by_model.get(column)
        cells.append(fmt_count(found.tokens) if found else "")
        return cells

    def table(shown: list[str]) -> list[str]:
        """Rows for these columns, each column only as wide as it needs, a space apart."""
        columns = shown + [TOTAL] + ([BILLED] if r.any_billed else [])
        grids = [column_cells(c) for c in columns]
        widths = [max(len(c) for c in cells) for cells in grids]
        names = [label, *(name for name, *_ in periods), SHARE, TOKENS]
        rows = []
        for i, name in enumerate(names):
            line = f"{name:<{LABEL}}" + " ".join(
                f"{cells[i]:>{w}}" for cells, w in zip(grids, widths, strict=True)
            )
            if i and (window := periods[i - 1][1] if i <= len(periods) else None):
                line += f"  +{fmt_count(window.unpriced)} unpriced" if window.unpriced else ""
            rows.append(line.rstrip())
        return [x for x in rows if x.strip() != TOKENS]  # no tokens row when there is none

    out = table(models)
    while len(models) > 1 and max(len(x) for x in out) > width:
        models.pop()
        out = table(models)
    return out


def plan_style(p: PlanUsage) -> str:
    """Amber from 70% of either limit, red from 90%; like the context line."""
    worst = max((lim.percent for lim in (p.five_hour, p.seven_day) if lim), default=0.0)
    return "bold red" if worst >= 90 else "bold dark_orange3" if worst >= 70 else ""


Line = tuple[str, str]  # the text of one line and its style
SCOPE_HEADERS = {"here": "Session", "subfolders": "Session · subfolders", "all": "Session · all"}


@dataclass(frozen=True)
class Heading:
    """The first group: what the mode line is about."""

    top: str  # the conversation's title, or the list's scope and size
    agent: str  # ``@ robot-name`` of the conversation; "" without one
    bottom: str  # its directory, or a hint


@dataclass(frozen=True)
class Selected:
    """The conversation the mode line describes; the default describes none."""

    ids: tuple[str, ...] = ()
    row: Row | None = None  # None: not in the list (yet)
    use: Use | None = None
    first_mb: float | None = None


@dataclass(frozen=True)
class Machine:
    """The rows of the CPU and memory table under the selected conversation's own."""

    everyone: str  # all running conversations together; "" when it equals "this one"
    app: str = ""  # the program that shows the list; "" to leave it out


@dataclass(frozen=True)
class Readings:
    """What the machine-wide sources last said."""

    plan_usage: PlanUsage | None = None
    plan_pace: plan.PlanPace | None = None
    upstreams: upstreams.View | None = None
    spend: spend.Report | None = None
    spend_label: str = ""
    intents: tuple[intents.Intent, ...] | None = None  # in scope; None: no group
    runners: tuple[jupyter.RunnerRow, ...] = ()
    uses: Mapping[int, Use] = field(default_factory=dict)


def compose(
    heading: Heading, selected: Selected, readings: Readings, machine: Machine | None, width: int
) -> list[list[Line]]:
    """The groups of the mode line, top to bottom. A group with nothing to say is left out.

    First the heading. Then the selected conversation's model and context, its last reply's
    effort and upstream, and its tokens. Then the plan limits, the gateway, the spend, the
    intents, the Jupyter runners, and the CPU and memory table (none when ``machine`` is None).
    The context and plan lines turn amber, then red, as the window fills.
    """
    row = selected.row
    context = fmt_context(row.tokens) if row else ""
    tokens = fmt_tokens(row.tokens, width) if row else ""
    effort = fmt_effort(row.tokens) if row else ""
    ctx_style = context_style(row.tokens) if row else ""
    agent: list[Line] = [(heading.agent, "bold")] if heading.agent else []
    groups: list[list[Line]] = [[(heading.top, ""), *agent, (heading.bottom, "")]]
    view = readings.upstreams
    serving = view.serving(selected.ids) if view else None
    if context or tokens:
        extra = [effort, fmt_upstream(serving) if serving else ""]
        between = [(x, "") for x in extra if x]
        groups.append([(context, ctx_style), *between, (tokens, "")])
    if readings.plan_usage:
        # another upstream serves this session: the limits are not what stops it now
        elsewhere = serving is not None and serving.kind != "passthrough"
        style = "dim" if elsewhere else plan_style(readings.plan_usage)
        lines = fmt_plan(readings.plan_usage, readings.plan_pace)
        groups.append([(x, style) for x in lines])
    if view:
        groups.append(fmt_gateway(view, serving.name if serving else ""))
    if readings.spend and readings.spend_label:
        lines = fmt_spend(readings.spend, readings.spend_label, width)
        groups.append([(x, "") for x in lines])
    if readings.intents is not None:
        groups.append(fmt_intents(readings.intents, time.time()))
    if live_runners := fmt_runners(readings.runners, readings.uses):
        groups.append(live_runners)
    if machine:
        this = fmt_use("this one", selected.use, selected.first_mb)
        rows = [USE_HEADER, this, machine.everyone, machine.app]
        groups.append([(x, "") for x in rows if x])
    return groups
