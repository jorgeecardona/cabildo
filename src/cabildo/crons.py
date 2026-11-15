"""Scheduled prompts that cabildo stores and the cabildo-push mod fires.

The mod hooks Claude Code's ``CronCreate``, ``CronDelete``, ``CronList`` and
``ScheduleWakeup`` tools (the tools ``/loop`` calls) and writes the job here
instead. Each job belongs to one session. The mod asks ``claim_due`` for the
jobs that are due and submits their prompts into that session.

Two kinds of job:

- ``cron``: a 5-field cron expression in local time, recurring or one-shot.
- ``wakeup``: a one-shot at a fixed moment, from ``ScheduleWakeup``.

Missed fires are coalesced: a job overdue by several matches fires once, and
its next fire is the first match after now. Recurring jobs never expire.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
import time
from typing import Any, Literal

JobKind = Literal["cron", "wakeup"]

# Prefix of the job id the model sees, so the mod can tell a cabildo job from a
# Claude Code job when ``CronDelete`` names one.
ID_PREFIX = "cab-"

# ScheduleWakeup's documented clamp.
WAKEUP_MIN_S = 60
WAKEUP_MAX_S = 3600

# How far ahead ``next_fire`` looks before it gives up on an expression that
# never matches (for example "0 0 31 2 *"). Four years covers 29 February.
_SEARCH_DAYS = 4 * 366


class CronError(ValueError):
    """A cron expression that cannot be parsed or never matches."""


# --------------------------------------------------------------------------- #
# Cron expressions
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class CronSpec:
    """A parsed 5-field cron expression.

    ``dom_any`` and ``dow_any`` record whether the field was ``*``. Standard cron
    fires on a day when either day field matches if both are restricted, and on
    the restricted one alone otherwise.
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_any: bool
    dow_any: bool

    def day_matches(self, day: dt.date) -> bool:
        """True when ``day`` passes the month and day-of-month/day-of-week fields."""
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.doms
        dow_ok = (day.isoweekday() % 7) in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok


def _field(text: str, lo: int, hi: int, name: str) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        step = 1
        rng = part
        if "/" in part:
            rng, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) == 0:
                msg = f"bad step in {name} field: {part!r}"
                raise CronError(msg)
            step = int(step_s)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                msg = f"bad range in {name} field: {part!r}"
                raise CronError(msg)
            start, end = int(a), int(b)
        elif rng.isdigit():
            start = int(rng)
            # "5/15" means from 5 to the end, every 15.
            end = hi if "/" in part else start
        else:
            msg = f"bad value in {name} field: {part!r}"
            raise CronError(msg)
        if start < lo or end > hi or start > end:
            msg = f"{name} field out of range {lo}-{hi}: {part!r}"
            raise CronError(msg)
        out.update(range(start, end + 1, step))
    return frozenset(out)


def parse(expr: str) -> CronSpec:
    """Parse ``"M H DoM Mon DoW"``. Day-of-week 7 is Sunday, like 0.

    Raises:
        CronError: the expression does not have 5 valid fields.
    """
    fields = expr.split()
    if len(fields) != 5:
        msg = f"cron needs 5 fields (M H DoM Mon DoW), got {len(fields)}: {expr!r}"
        raise CronError(msg)
    m, h, dom, mon, dow = fields
    dows = _field(dow, 0, 7, "day-of-week")
    if 7 in dows:
        dows = (dows - {7}) | {0}
    return CronSpec(
        minutes=_field(m, 0, 59, "minute"),
        hours=_field(h, 0, 23, "hour"),
        doms=_field(dom, 1, 31, "day-of-month"),
        months=_field(mon, 1, 12, "month"),
        dows=dows,
        dom_any=dom == "*",
        dow_any=dow == "*",
    )


def next_fire(spec: CronSpec, after: float) -> float:
    """The first match strictly after ``after`` (epoch seconds), in local time.

    Raises:
        CronError: no match within four years.
    """
    start = dt.datetime.fromtimestamp(after).replace(second=0, microsecond=0)
    start += dt.timedelta(minutes=1)
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)
    day = start.date()
    for offset in range(_SEARCH_DAYS):
        if spec.day_matches(day):
            first_day = offset == 0
            for h in hours:
                if first_day and h < start.hour:
                    continue
                for m in minutes:
                    if first_day and h == start.hour and m < start.minute:
                        continue
                    # Cron is local wall-clock time, so the naive datetime is the intent.
                    return dt.datetime(day.year, day.month, day.day, h, m).timestamp()  # noqa: DTZ001
        day += dt.timedelta(days=1)
    msg = "cron expression never matches"
    raise CronError(msg)


def humanize(expr: str) -> str:
    """A short English description of common expressions; the expression otherwise."""
    f = expr.split()
    if len(f) != 5:
        return expr
    m, h, dom, mon, dow = f
    rest_any = dom == "*" and mon == "*" and dow == "*"
    if rest_any and h == "*":
        if m == "*":
            return "every minute"
        if m.startswith("*/") and m[2:].isdigit():
            return f"every {int(m[2:])} minutes"
        if m.isdigit():
            return f"every hour at :{int(m):02d}"
    if rest_any and m.isdigit() and h.startswith("*/") and h[2:].isdigit():
        return f"every {int(h[2:])} hours at :{int(m):02d}"
    if rest_any and m.isdigit() and h.isdigit():
        return f"every day at {int(h):02d}:{int(m):02d}"
    return f"cron {expr}"


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Job:
    """One stored job, as the mod and the CLI see it."""

    id: int
    session_id: str
    project: str | None
    kind: JobKind
    cron: str | None
    prompt: str
    recurring: bool
    next_fire_at: float
    created_at: float
    last_fired_at: float | None
    fire_count: int
    reason: str | None

    @property
    def public_id(self) -> str:
        """The id the model sees: ``cab-<row id>``."""
        return f"{ID_PREFIX}{self.id}"

    @property
    def human_schedule(self) -> str:
        """What ``CronCreate`` reports as ``humanSchedule``."""
        at = dt.datetime.fromtimestamp(self.next_fire_at)
        if self.cron is None:
            return f"once at {at:%H:%M:%S}"
        if not self.recurring:
            return f"once at {at:%Y-%m-%d %H:%M}"
        return humanize(self.cron)

    def to_json(self) -> dict[str, Any]:
        """The JSON shape the CLI prints; times in epoch milliseconds, as Claude Code uses."""
        return {
            "id": self.public_id,
            "session_id": self.session_id,
            "project": self.project,
            "kind": self.kind,
            "cron": self.cron,
            "humanSchedule": self.human_schedule,
            "prompt": self.prompt,
            "recurring": self.recurring,
            "nextFireAt": int(self.next_fire_at * 1000),
            "createdAt": int(self.created_at * 1000),
            "lastFiredAt": None if self.last_fired_at is None else int(self.last_fired_at * 1000),
            "fireCount": self.fire_count,
            "reason": self.reason,
        }


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        session_id=row["session_id"],
        project=row["project"],
        kind=row["kind"],
        cron=row["cron"],
        prompt=row["prompt"],
        recurring=bool(row["recurring"]),
        next_fire_at=row["next_fire_at"],
        created_at=row["created_at"],
        last_fired_at=row["last_fired_at"],
        fire_count=row["fire_count"],
        reason=row["reason"],
    )


def parse_id(public_id: str) -> int | None:
    """The row id of ``cab-<n>`` (or a bare ``<n>``); None for any other id."""
    s = public_id.strip()
    s = s.removeprefix(ID_PREFIX)
    return int(s) if s.isdigit() else None


def _insert(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    project: str | None,
    kind: JobKind,
    cron: str | None,
    prompt: str,
    recurring: bool,
    next_fire_at: float,
    reason: str | None,
    now: float,
) -> Job:
    cur = conn.execute(
        "INSERT INTO crons(session_id, project, kind, cron, prompt, recurring, next_fire_at, "
        "created_at, reason) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, project, kind, cron, prompt, int(recurring), next_fire_at, now, reason),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM crons WHERE id=?", (cur.lastrowid,)).fetchone()
    return _job(row)


def add_cron(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    cron: str,
    prompt: str,
    recurring: bool = True,
    project: str | None = None,
    now: float | None = None,
) -> Job:
    """Store a cron job and return it, with its first fire time computed.

    Raises:
        CronError: the expression is invalid or never matches.
    """
    now = time.time() if now is None else now
    first = next_fire(parse(cron), now)
    return _insert(
        conn,
        session_id=session_id,
        project=project,
        kind="cron",
        cron=cron,
        prompt=prompt,
        recurring=recurring,
        next_fire_at=first,
        reason=None,
        now=now,
    )


@dataclasses.dataclass(frozen=True)
class Wakeup:
    """A stored wakeup and the clamp ``ScheduleWakeup`` reports."""

    job: Job
    clamped_delay_s: int
    was_clamped: bool


def add_wakeup(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    delay_s: float,
    prompt: str,
    reason: str | None = None,
    project: str | None = None,
    now: float | None = None,
) -> Wakeup:
    """Store a one-shot wakeup ``delay_s`` from now, clamped to 60-3600 s like Claude Code."""
    now = time.time() if now is None else now
    clamped = int(min(max(round(delay_s), WAKEUP_MIN_S), WAKEUP_MAX_S))
    job = _insert(
        conn,
        session_id=session_id,
        project=project,
        kind="wakeup",
        cron=None,
        prompt=prompt,
        recurring=False,
        next_fire_at=now + clamped,
        reason=reason,
        now=now,
    )
    return Wakeup(job=job, clamped_delay_s=clamped, was_clamped=clamped != delay_s)


def list_jobs(conn: sqlite3.Connection, session_id: str | None = None) -> list[Job]:
    """Jobs of one session, or of every session when ``session_id`` is None; soonest first."""
    if session_id is None:
        rows = conn.execute("SELECT * FROM crons ORDER BY next_fire_at").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM crons WHERE session_id=? ORDER BY next_fire_at", (session_id,)
        ).fetchall()
    return [_job(r) for r in rows]


def delete_job(conn: sqlite3.Connection, job_id: int, session_id: str | None = None) -> bool:
    """Delete one job. With ``session_id``, only if that session owns it. True if deleted."""
    if session_id is None:
        cur = conn.execute("DELETE FROM crons WHERE id=?", (job_id,))
    else:
        cur = conn.execute("DELETE FROM crons WHERE id=? AND session_id=?", (job_id, session_id))
    conn.commit()
    return cur.rowcount > 0


def delete_wakeups(conn: sqlite3.Connection, session_id: str) -> int:
    """Delete the session's pending wakeups (``ScheduleWakeup(stop=true)``). Returns the count."""
    cur = conn.execute("DELETE FROM crons WHERE session_id=? AND kind='wakeup'", (session_id,))
    conn.commit()
    return cur.rowcount


def move_jobs(conn: sqlite3.Connection, from_session: str, to_session: str) -> int:
    """Give every job of ``from_session`` to ``to_session`` (a ``/clear`` changes the id)."""
    cur = conn.execute(
        "UPDATE crons SET session_id=? WHERE session_id=?", (to_session, from_session)
    )
    conn.commit()
    return cur.rowcount


def claim_due(conn: sqlite3.Connection, session_id: str, *, now: float | None = None) -> list[Job]:
    """Return the session's due jobs and mark them fired, in one transaction.

    A one-shot job is deleted. A recurring job moves to its first match after
    ``now``, so missed matches fire once. A second caller in the same instant
    gets nothing: the claim and the update happen under one write lock.
    """
    now = time.time() if now is None else now
    fired: list[Job] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM crons WHERE session_id=? AND next_fire_at<=? ORDER BY next_fire_at",
            (session_id, now),
        ).fetchall()
        for row in rows:
            job = _job(row)
            fired.append(job)
            if not job.recurring or job.cron is None:
                conn.execute("DELETE FROM crons WHERE id=?", (job.id,))
                continue
            try:
                nxt = next_fire(parse(job.cron), now)
            except CronError:
                conn.execute("DELETE FROM crons WHERE id=?", (job.id,))
                continue
            conn.execute(
                "UPDATE crons SET next_fire_at=?, last_fired_at=?, fire_count=fire_count+1 "
                "WHERE id=?",
                (nxt, now, job.id),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return fired


def next_due_at(conn: sqlite3.Connection, session_id: str) -> float | None:
    """The soonest ``next_fire_at`` of the session's jobs; None when it has none."""
    row = conn.execute(
        "SELECT MIN(next_fire_at) AS t FROM crons WHERE session_id=?", (session_id,)
    ).fetchone()
    return None if row is None or row["t"] is None else float(row["t"])
