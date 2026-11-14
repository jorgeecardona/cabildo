"""What the API key has consumed, for the mode-line row a subscription spends on limits.

A subscription has a published percentage to show (``plan.py``); an API key has no such
endpoint, only a bill that arrives later. What it does have is the transcripts: every
assistant message carries the tokens its request billed, so the consumption is already on
disk and only needs adding up and pricing (``cabildo.usage``, ``cabildo.prices``).

Two things this is careful about. It reports *this machine's* consumption, not the
account's: another laptop on the same key spends against the same bill and leaves no
trace here, so the figure is a floor, not a statement of the invoice. And tokens are
always shown, dollars only where the price table knows the model, because a confidently
wrong dollar figure is worse than an honest token count.

When an Admin API key is configured (``cabildo.billing``), the organization's billed
cost is loaded alongside it and shown in its own column. It is the authoritative number
and it does not replace the computed one: it covers the whole organization rather than
this machine, and it lags by hours, so today's billed cost is usually absent exactly
when someone looks. Two columns, each labelled, beats one column whose meaning changes
by row.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cabildo import billing, store, usage
from cabildo.workspace import heatmap

#: How often the transcripts are re-read. Ingestion reads only the bytes appended since
#: last time, so this is cheap after the first pass; the first pass on a long history is
#: seconds, which is why it runs on a worker thread and never on the UI one.
REFRESH_SECONDS = 60.0

#: Re-exported so a caller needs one import for the loader and the types it returns, and
#: so `cabildo.usage` (tokens) is not confused with `cabildo.workspace.usage` (CPU and
#: memory), which the app already imports under that name.
Spend = usage.Spend
Window = usage.Window

#: A write to the bus DB waits this long for another writer before giving up. The mode
#: line can show the previous numbers for a minute; it cannot block on a lock.
TIMEOUT = 5.0


@dataclass(frozen=True)
class Report:
    """What the mode line shows: the computed windows, and the billed ones when known.

    ``billed_*`` is None where the cost API has nothing for that window -- no credential,
    nothing fetched yet, or, for today, data that has not landed. None is not zero and is
    rendered as a dash, never as ``$0.00``.

    ``session`` is the shown conversation's own consumption over the same three periods,
    so a long conversation's cost is readable against the machine's.

    ``models`` splits today by model. It is the one place tokens are reported, because a
    token only means something next to others of its own model.
    """

    spend: usage.Spend
    billed_today: float | None = None
    billed_week: float | None = None
    billed_all: float | None = None
    session: usage.Spend | None = None  # the shown conversation's share; None when none
    models: tuple[usage.ModelSpend, ...] = ()  # today, per model, dearest first

    @property
    def any_billed(self) -> bool:
        return any(x is not None for x in (self.billed_today, self.billed_week, self.billed_all))


def billed_windows(conn, cwd: str | None, at: float) -> tuple[float | None, ...]:
    """(today, 7 days, all time) as the cost API reports them; each None when unknown.

    Today is None until the API's data horizon has passed local midnight. Before that the
    day is partial, and a partial figure in a column headed "billed" would read as the
    day's cost rather than as the fraction of it that has landed.
    """
    found = billing.refresh(conn, cwd, datetime.fromtimestamp(at, UTC))
    if not found.days:
        return (None, None, None)
    midnight = (
        datetime.fromtimestamp(at).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    )
    horizon = found.refreshed_at
    today = found.since(midnight) if horizon and horizon >= midnight else None
    return (today, found.since(datetime.fromtimestamp(at - 7 * 86400, UTC)), found.total())


def load(
    now: float | None = None,
    cwd: str | None = None,
    sessions: tuple[str, ...] = (),
) -> Report | None:
    """Bring the usage table up to date with the transcripts and return both figures.

    None when there is no bus DB yet, or when the DB cannot be read or written: the mode
    line then keeps whatever it last showed. Never raises -- a spend figure is worth less
    than the workspace it would take down.
    """
    at = time.time() if now is None else now
    if not store.db_path().exists():
        return None
    try:
        conn = store.connect()
    except sqlite3.Error:
        return None
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(TIMEOUT * 1000)}")
        try:
            usage.ingest(conn)
        except sqlite3.Error:
            pass  # another writer holds it: price what is already recorded
        computed = usage.spend(conn, at)
        mine = usage.spend(conn, at, sessions) if sessions else None
        models = tuple(usage.by_model(conn, usage._midnight(at)))
        try:
            today, week, all_time = billed_windows(conn, cwd, at)
        except sqlite3.Error:
            today = week = all_time = None  # the billed column is an extra; computed is not
        return Report(computed, today, week, all_time, mine, models)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def calendar(today: dt.date | None = None) -> heatmap.Calendar | None:
    """A year of token use per day, for the activity screen. None without a bus DB.

    Ingests first, like `load`: the screen is often the first thing opened in a session,
    and a calendar missing today's work would be the one thing anyone checks.
    """
    if not store.db_path().exists():
        return None
    try:
        conn = store.connect()
    except sqlite3.Error:
        return None
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(TIMEOUT * 1000)}")
        try:
            usage.ingest(conn)
        except sqlite3.Error:
            pass
        per_day = heatmap.by_day(conn, 0.0)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return heatmap.build(per_day, today or dt.date.today())
