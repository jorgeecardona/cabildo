"""The wind-down half of the attention engine: a stop line the human sets, and
the schedule-anchored nudge every governed session hears as that line nears.

The contract pinned here is the one that decides whether the nudge is bearable
to leave on:

  * nothing is said outside the wrap-up window (no 3pm nag against a 23:00 stop);
  * each escalation step lands at most once per session;
  * a global line governs every session; a project line governs only its own;
  * a newer stop for a scope replaces the old one rather than stacking.
"""

from __future__ import annotations

import time

import pytest

from cabildo import notify, store
from cabildo.cli import _parse_stop_time
from tests.conftest import make_session


@pytest.fixture
def now():
    return time.time()


def test_silent_without_a_stop(conn):
    make_session(conn, "s1")
    assert notify.winddown_text(conn, "s1") is None


def test_outside_the_window_is_silent(conn, now):
    make_session(conn, "s1")
    store.set_stop(conn, line_at=now + 5 * 3600, scope="global")
    # five hours out is step 0: the afternoon stays quiet.
    assert notify.winddown_text(conn, "s1") is None


def test_escalates_through_the_steps_once_each(conn, now):
    make_session(conn, "s1")

    store.set_stop(conn, line_at=now + 40 * 60, scope="global")
    first = notify.winddown_text(conn, "s1")
    assert first and "min to your" in first
    # same step, next tool call: silent.
    assert notify.winddown_text(conn, "s1") is None

    store.set_stop(conn, line_at=now + 10 * 60, scope="global")
    land = notify.winddown_text(conn, "s1")
    assert land and "don't start" in land

    store.set_stop(conn, line_at=now - 5 * 60, scope="global")
    over = notify.winddown_text(conn, "s1")
    assert over and "past your" in over
    assert notify.winddown_text(conn, "s1") is None


def test_global_line_governs_every_session(conn, now):
    make_session(conn, "a", project="/repo/alpha", project_name="alpha")
    make_session(conn, "b", project="/repo/beta", project_name="beta")
    store.set_stop(conn, line_at=now + 10 * 60, scope="global")
    assert notify.winddown_text(conn, "a") is not None
    assert notify.winddown_text(conn, "b") is not None


def test_project_line_governs_only_its_project(conn, now):
    make_session(conn, "a", project="/repo/alpha", project_name="alpha")
    make_session(conn, "b", project="/repo/beta", project_name="beta")
    store.set_stop(conn, line_at=now + 10 * 60, scope="/repo/alpha")
    assert notify.winddown_text(conn, "a") is not None
    assert notify.winddown_text(conn, "b") is None


def test_soonest_line_wins(conn, now):
    make_session(conn, "a", project="/repo/alpha", project_name="alpha")
    store.set_stop(conn, line_at=now + 10 * 60, scope="/repo/alpha")
    store.set_stop(conn, line_at=now + 40 * 60, scope="global")
    gov = store.governing_stop(conn, "a")
    assert gov is not None and gov["scope"] == "/repo/alpha"


def test_new_stop_replaces_old_for_same_scope(conn, now):
    store.set_stop(conn, line_at=now + 10 * 60, scope="global")
    store.set_stop(conn, line_at=now + 40 * 60, scope="global")
    active = store.active_stops(conn)
    assert len(active) == 1
    assert active[0]["line_at"] == pytest.approx(now + 40 * 60)


def test_clear_stands_it_down(conn, now):
    make_session(conn, "s1")
    store.set_stop(conn, line_at=now + 10 * 60, scope="global")
    assert store.clear_stops(conn) == 1
    assert store.active_stops(conn) == []
    assert notify.winddown_text(conn, "s1") is None


def test_stale_line_is_ignored(conn, now):
    make_session(conn, "s1")
    # a stop from long before today: past the stale horizon, no longer honoured.
    store.set_stop(conn, line_at=now - store.SCHEDULE_STALE_AFTER - 60, scope="global")
    assert store.active_stops(conn) == []
    assert notify.winddown_text(conn, "s1") is None


@pytest.mark.parametrize(
    "text, hour, minute",
    [
        ("23:00", 23, 0),
        ("23", 23, 0),
        ("11pm", 23, 0),
        ("9:30pm", 21, 30),
        ("12am", 0, 0),
        ("12pm", 12, 0),
        ("8:05", 8, 5),
    ],
)
def test_parse_stop_time(text, hour, minute):
    from datetime import datetime

    ts = _parse_stop_time(text)
    assert ts is not None
    got = datetime.fromtimestamp(ts).astimezone()
    assert (got.hour, got.minute) == (hour, minute)
    assert ts > time.time()  # always resolves to a future instant


@pytest.mark.parametrize("bad", ["99:99", "nope", "", "25", "7:70"])
def test_parse_stop_time_rejects_garbage(bad):
    assert _parse_stop_time(bad) is None
