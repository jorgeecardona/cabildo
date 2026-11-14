"""A year of token use as a calendar, the shape every contribution graph uses.

One cell per day, weeks as columns and weekdays as rows, so a year is one glance: which
weeks were heavy, which days were quiet, where the gaps are. The shade is estimated
dollars, because tokens of different models are not the same amount: a Haiku token and a
Fable token are 80 times apart in price, and a sum of them says little.

Levels are quartiles of the days that had any use, not fixed thresholds: a week of heavy
work and a week of light work should both show their own range, and an absolute scale
would paint one of them a flat colour. A day with nothing is its own level, distinct from
the lightest day that had something.

Pure: it returns text and level numbers and knows nothing about Textual or curses, so it
is testable without a terminal.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: Shades from "nothing" to "most". Five, like every graph of this shape, because more
#: steps than that are not distinguishable at one character per day.
LEVELS = 5
EMPTY, LIGHTEST, MOST = 0, 1, LEVELS - 1

#: Weeks shown. 53 covers a year and the part-week at each end.
WEEKS = 53
DAYS = 7

CELL = "■"
BLANK = " "

#: Weekday rows are labelled like the published graphs: every other day, Monday first.
ROW_LABELS = ("M", "", "W", "", "F", "", "")


def by_day(conn: sqlite3.Connection, since: float) -> dict[str, float]:
    """Estimated dollars per local calendar day, from ``since`` (unix seconds) onward.

    Dollars, not tokens: a Haiku token and a Fable token are not the same amount of work
    or money, and a sum of them says little. Local, not UTC, to agree with the mode line's
    "today": a day is the one whose work the human remembers doing.
    """
    from cabildo import usage

    return usage.cost_by_day(conn, since)


def thresholds(values: Sequence[float]) -> list[float]:
    """The quartile cuts of the days that had any use, smallest first.

    Returns ``LEVELS - 2`` cuts: a day at or above cut *i* is at least level *i* + 2.
    An empty history has no cuts, and every day then reads as the lightest level.
    """
    used = sorted(v for v in values if v > 0)
    if not used:
        return []
    cuts = []
    for i in range(1, LEVELS - 1):
        at = (len(used) - 1) * i // (LEVELS - 1)
        cuts.append(used[at])
    return cuts


def level(value: float, cuts: list[float]) -> int:
    """Which shade a day's token count falls in: 0 for nothing, 1..LEVELS-1 otherwise."""
    if value <= 0:
        return EMPTY
    found = LIGHTEST
    for i, cut in enumerate(cuts):
        if value >= cut:
            found = i + 2
    return min(found, MOST)


@dataclass(frozen=True)
class Cell:
    """One day: when it was, what it cost, and how dark it reads."""

    day: str  # YYYY-MM-DD; "" for a padding cell before the first week starts
    value: float  # estimated dollars
    level: int

    @property
    def char(self) -> str:
        return BLANK if not self.day else CELL


@dataclass(frozen=True)
class Calendar:
    """``WEEKS`` columns of ``DAYS`` cells, Monday at the top, oldest week first."""

    weeks: tuple[tuple[Cell, ...], ...]
    months: tuple[tuple[int, str], ...]  # (column, "Nov") where a month's first week sits
    total: float

    def rows(self) -> list[list[Cell]]:
        """The same grid by weekday, which is the order a terminal draws it in."""
        return [[week[d] for week in self.weeks] for d in range(DAYS)]


def build(per_day: Mapping[str, float], today: dt.date, weeks: int = WEEKS) -> Calendar:
    """The calendar ending on ``today``'s week, with ``weeks`` columns.

    The last column is the current, part-finished week: its days after today are padding,
    not zeros, so an unfinished week does not read as a run of idle days.
    """
    end = today + dt.timedelta(days=6 - today.weekday())  # the Sunday closing this week
    start = end - dt.timedelta(days=weeks * DAYS - 1)  # a Monday
    cuts = thresholds(list(per_day.values()))
    grid, months, seen = [], [], set()
    total = 0.0
    for w in range(weeks):
        column = []
        for d in range(DAYS):
            date = start + dt.timedelta(days=w * DAYS + d)
            if date > today:
                column.append(Cell("", 0.0, EMPTY))
                continue
            key = date.isoformat()
            value = per_day.get(key, 0.0)
            total += value
            column.append(Cell(key, value, level(value, cuts)))
        grid.append(tuple(column))
        first = start + dt.timedelta(days=w * DAYS)
        if first.month not in seen or first.day <= DAYS:
            if (first.year, first.month) not in seen:
                months.append((w, first.strftime("%b")))
                seen.add((first.year, first.month))
    return Calendar(tuple(grid), tuple(months), total)


def month_line(cal: Calendar, cell_width: int = 1) -> str:
    """The month labels above the grid, each over the week its month begins in."""
    line = ""
    for column, name in cal.months:
        at = column * cell_width
        if at < len(line):
            continue  # the previous label already runs past here
        line += BLANK * (at - len(line)) + name
    return line
