"""``heatmap``: a year of token use as a calendar, and the shading that makes it readable."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from cabildo.workspace import heatmap

MONDAY = dt.date(2026, 11, 9)
SUNDAY = dt.date(2026, 11, 15)


def test_the_grid_is_weeks_of_seven_days_ending_on_this_week():
    cal = heatmap.build({}, MONDAY, weeks=3)
    assert len(cal.weeks) == 3
    assert all(len(week) == heatmap.DAYS for week in cal.weeks)
    # the last column is this week, and its first day is the Monday of it
    assert cal.weeks[-1][0].day == MONDAY.isoformat()


def test_days_after_today_are_padding_not_idle_days():
    """An unfinished week must not read as a run of quiet days: the cells after today
    carry no date and draw as blank.
    """
    cal = heatmap.build({}, dt.date(2026, 11, 11), weeks=1)  # a Wednesday
    week = cal.weeks[-1]
    assert [c.day != "" for c in week] == [True, True, True, False, False, False, False]
    assert week[-1].char == heatmap.BLANK
    assert week[0].char == heatmap.CELL


def test_a_day_with_nothing_is_its_own_level():
    cal = heatmap.build({MONDAY.isoformat(): 1000}, SUNDAY, weeks=1)
    levels = {c.day: c.level for c in cal.weeks[-1]}
    assert levels[MONDAY.isoformat()] >= heatmap.LIGHTEST
    assert levels[(MONDAY + dt.timedelta(days=1)).isoformat()] == heatmap.EMPTY


def test_shades_are_quartiles_of_the_days_that_had_use():
    """Fixed thresholds would paint a light week and a heavy week the same flat colour.
    The cuts come from the data, so both weeks show their own range.
    """
    cuts = heatmap.thresholds([0, 0, 10, 20, 30, 40, 50, 60, 70, 80])
    assert cuts == sorted(cuts) and len(cuts) == heatmap.LEVELS - 2
    assert heatmap.level(10, cuts) == heatmap.LIGHTEST
    assert heatmap.level(80, cuts) == heatmap.MOST
    assert heatmap.level(0, cuts) == heatmap.EMPTY
    assert heatmap.LIGHTEST < heatmap.level(40, cuts) < heatmap.MOST


def test_one_busy_day_does_not_make_every_other_day_the_lightest_shade():
    """Quantiles, not a share of the maximum: a single outlier would flatten the rest."""
    days = [100, 200, 300, 400, 10_000_000]
    cuts = heatmap.thresholds(days)
    assert len({heatmap.level(d, cuts) for d in days}) > 2


def test_a_history_with_no_use_still_draws():
    assert heatmap.thresholds([]) == []
    assert heatmap.level(5, []) == heatmap.LIGHTEST


def test_each_month_is_labelled_over_the_week_it_starts_in():
    cal = heatmap.build({}, dt.date(2026, 3, 15), weeks=12)
    names = [name for _, name in cal.months]
    assert names == sorted(set(names), key=names.index)  # no month twice in a row
    assert "Mar" in names
    line = heatmap.month_line(cal, cell_width=2)
    assert line.index("Mar") == next(c for c, n in cal.months if n == "Mar") * 2


def test_labels_never_overlap_each_other():
    cal = heatmap.build({}, dt.date(2026, 11, 14), weeks=heatmap.WEEKS)
    line = heatmap.month_line(cal, cell_width=2)
    assert "  " in line  # the labels are spaced, not run together
    for _, name in cal.months:
        assert name in line


def test_the_total_counts_only_days_on_the_grid():
    per_day = {MONDAY.isoformat(): 500, "2020-01-01": 999_999}
    cal = heatmap.build(per_day, SUNDAY, weeks=1)
    assert cal.total == 500


def test_rows_are_weekdays_across_the_weeks():
    cal = heatmap.build({}, SUNDAY, weeks=4)
    rows = cal.rows()
    assert len(rows) == heatmap.DAYS
    assert all(len(row) == 4 for row in rows)
    assert rows[0][0] is cal.weeks[0][0]


def test_by_day_buckets_on_the_local_calendar(conn):
    """The mode line's "today" is the local day, so the calendar's must be too -- a late
    evening's work belongs to the day the human remembers, not to the next UTC one.
    """
    # 23:30 local on 2026-11-13, which is already the 14th in UTC east of Greenwich
    local = time.mktime((2026, 11, 13, 23, 30, 0, 0, 0, -1))
    conn.execute(
        "INSERT INTO usage_messages (message_id, ts, session_id, agent_id, project, model, "
        "input, output, cache_read, cache_write) VALUES ('m', ?, 's', NULL, 'p', 'x', 1,2,3,4)",
        (local,),
    )
    conn.commit()
    # an unknown model cannot be priced: the day still shows, at the lightest shade
    assert heatmap.by_day(conn, 0.0) == {"2026-11-13": pytest.approx(1e-9)}


def test_a_day_on_a_cheap_model_shades_lighter_than_one_on_an_expensive_one(conn):
    """The reason the calendar is in dollars: the same token count on Haiku and on Fable
    is not the same amount, and a token total would paint both days alike.
    """
    day1 = time.mktime((2026, 11, 12, 12, 0, 0, 0, 0, -1))
    day2 = time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))
    for ts, model in ((day1, "claude-haiku-5-5"), (day2, "claude-fable-5-2")):
        conn.execute(
            "INSERT INTO usage_messages (message_id, ts, session_id, agent_id, project, model, "
            "input, output, cache_read, cache_write) VALUES (?, ?, 's', NULL, 'p', ?, "
            "50000, 50000, 0, 0)",  # under Haiku 5.5's 100K tier line
            (model, ts, model),
        )
    conn.commit()
    got = heatmap.by_day(conn, 0.0)
    assert got["2026-11-13"] > 50 * got["2026-11-12"]
