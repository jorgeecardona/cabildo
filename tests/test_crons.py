"""Scheduled prompts stored in cabildo (crons.py) and the `cabildo cron` CLI."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from cabildo import cli, crons

from .conftest import make_session


def _at(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> dt.datetime:
    # Cron runs on local wall-clock time, so the tests use naive local datetimes.
    return dt.datetime(y, mo, d, h, mi)  # noqa: DTZ001


def _ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> float:
    return _at(y, mo, d, h, mi).timestamp()


def _local(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t)


# --------------------------------------------------------------------------- #
# Parser and next fire
# --------------------------------------------------------------------------- #
def test_every_five_minutes_fires_on_the_next_multiple():
    t = crons.next_fire(crons.parse("*/5 * * * *"), _ts(2026, 11, 15, 18, 22))
    assert _local(t) == _at(2026, 11, 15, 18, 25)


def test_next_fire_is_strictly_after_a_matching_minute():
    t = crons.next_fire(crons.parse("*/5 * * * *"), _ts(2026, 11, 15, 18, 25))
    assert _local(t) == _at(2026, 11, 15, 18, 30)


def test_daily_time_rolls_to_tomorrow():
    t = crons.next_fire(crons.parse("30 9 * * *"), _ts(2026, 11, 15, 10, 0))
    assert _local(t) == _at(2026, 11, 16, 9, 30)


def test_dom_and_dow_both_restricted_match_either():
    # 1st of the month OR Monday. 2026-11-15 is a Sunday; the 16th is a Monday.
    t = crons.next_fire(crons.parse("0 8 1 * 1"), _ts(2026, 11, 15, 12, 0))
    assert _local(t) == _at(2026, 11, 16, 8, 0)


def test_dow_seven_is_sunday():
    t = crons.next_fire(crons.parse("0 8 * * 7"), _ts(2026, 11, 16, 12, 0))
    assert _local(t) == _at(2026, 11, 22, 8, 0)


def test_feb_29_is_found_within_four_years():
    t = crons.next_fire(crons.parse("0 0 29 2 *"), _ts(2026, 11, 15))
    assert _local(t) == _at(2028, 2, 29, 0, 0)


@pytest.mark.parametrize(
    "expr", ["bad", "* * * *", "60 * * * *", "*/0 * * * *", "5-1 * * * *", "a * * * *"]
)
def test_invalid_expressions_raise(expr):
    with pytest.raises(crons.CronError):
        crons.parse(expr)


def test_expression_that_never_matches_raises():
    with pytest.raises(crons.CronError):
        crons.next_fire(crons.parse("0 0 31 2 *"), _ts(2026, 11, 15))


@pytest.mark.parametrize(
    ("expr", "text"),
    [
        ("* * * * *", "every minute"),
        ("*/5 * * * *", "every 5 minutes"),
        ("7 * * * *", "every hour at :07"),
        ("0 */2 * * *", "every 2 hours at :00"),
        ("30 9 * * *", "every day at 09:30"),
        ("0 8 * * 1", "cron 0 8 * * 1"),
    ],
)
def test_humanize(expr, text):
    assert crons.humanize(expr) == text


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def test_recurring_job_fires_once_for_missed_matches_and_moves_forward(conn):
    make_session(conn, "s1")
    t0 = _ts(2026, 11, 15, 18, 0)
    job = crons.add_cron(conn, session_id="s1", cron="*/5 * * * *", prompt="p", now=t0)
    assert _local(job.next_fire_at) == _at(2026, 11, 15, 18, 5)

    # 17 minutes later three matches were missed; it fires once.
    fired = crons.claim_due(conn, "s1", now=_ts(2026, 11, 15, 18, 17))
    assert [j.id for j in fired] == [job.id]
    (after,) = crons.list_jobs(conn, "s1")
    assert _local(after.next_fire_at) == _at(2026, 11, 15, 18, 20)
    assert after.fire_count == 1

    # Nothing more is due at the same instant.
    assert crons.claim_due(conn, "s1", now=_ts(2026, 11, 15, 18, 17)) == []


def test_one_shot_job_is_deleted_after_it_fires(conn):
    make_session(conn, "s1")
    t0 = _ts(2026, 11, 15, 18, 0)
    crons.add_cron(conn, session_id="s1", cron="30 18 * * *", prompt="p", recurring=False, now=t0)
    assert crons.claim_due(conn, "s1", now=_ts(2026, 11, 15, 18, 29)) == []
    assert len(crons.claim_due(conn, "s1", now=_ts(2026, 11, 15, 18, 30))) == 1
    assert crons.list_jobs(conn, "s1") == []


def test_due_jobs_belong_to_their_session(conn):
    make_session(conn, "s1")
    make_session(conn, "s2", claude_pid=1001)
    t0 = _ts(2026, 11, 15, 18, 0)
    crons.add_cron(conn, session_id="s1", cron="* * * * *", prompt="p", now=t0)
    assert crons.claim_due(conn, "s2", now=t0 + 600) == []
    assert len(crons.claim_due(conn, "s1", now=t0 + 600)) == 1


def test_wakeup_is_clamped_like_schedule_wakeup(conn):
    make_session(conn, "s1")
    low = crons.add_wakeup(conn, session_id="s1", delay_s=5, prompt="p", now=100.0)
    high = crons.add_wakeup(conn, session_id="s1", delay_s=9000, prompt="p", now=100.0)
    ok = crons.add_wakeup(conn, session_id="s1", delay_s=120, prompt="p", now=100.0)
    assert (low.clamped_delay_s, low.was_clamped) == (60, True)
    assert (high.clamped_delay_s, high.was_clamped) == (3600, True)
    assert (ok.clamped_delay_s, ok.was_clamped) == (120, False)
    assert ok.job.next_fire_at == 220.0


def test_stop_cancels_wakeups_but_keeps_cron_jobs(conn):
    make_session(conn, "s1")
    crons.add_wakeup(conn, session_id="s1", delay_s=120, prompt="p")
    crons.add_cron(conn, session_id="s1", cron="*/5 * * * *", prompt="p")
    assert crons.delete_wakeups(conn, "s1") == 1
    assert [j.kind for j in crons.list_jobs(conn, "s1")] == ["cron"]


def test_delete_with_session_refuses_another_sessions_job(conn):
    make_session(conn, "s1")
    job = crons.add_cron(conn, session_id="s1", cron="*/5 * * * *", prompt="p")
    assert not crons.delete_job(conn, job.id, "s2")
    assert crons.delete_job(conn, job.id, "s1")


def test_move_gives_jobs_to_the_new_session(conn):
    make_session(conn, "old")
    crons.add_cron(conn, session_id="old", cron="*/5 * * * *", prompt="p")
    assert crons.move_jobs(conn, "old", "new") == 1
    assert len(crons.list_jobs(conn, "new")) == 1


def test_one_shot_cron_says_once(conn):
    make_session(conn, "s1")
    t0 = _ts(2026, 11, 15, 18, 0)
    job = crons.add_cron(
        conn, session_id="s1", cron="37 18 * * *", prompt="p", recurring=False, now=t0
    )
    assert job.human_schedule == "once at 2026-11-15 18:37"


def test_parse_id():
    assert crons.parse_id("cab-12") == 12
    assert crons.parse_id("12") == 12
    assert crons.parse_id("a1b2c3d4") is None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_add_list_due_rm(conn, capsys):
    make_session(conn, "s1")
    argv = ["cron", "add", "--session=s1", "--cron", "*/5 * * * *", "--json", "--", "go"]
    assert cli.main(argv) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["id"].startswith("cab-")
    assert added["humanSchedule"] == "every 5 minutes"

    assert cli.main(["cron", "list", "--session=s1", "--json"]) == 0
    assert [j["prompt"] for j in json.loads(capsys.readouterr().out)["jobs"]] == ["go"]

    assert cli.main(["cron", "due", "--session=s1"]) == 0
    due = json.loads(capsys.readouterr().out)
    assert due["jobs"] == []
    assert due["nextFireAt"] == added["nextFireAt"]

    assert cli.main(["cron", "rm", added["id"], "--session=s1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] is True


def test_cli_rejects_a_bad_expression(conn, capsys):
    make_session(conn, "s1")
    assert cli.main(["cron", "add", "--session=s1", "--cron", "nope", "--", "go"]) == 1
    assert "5 fields" in capsys.readouterr().err


def test_cli_unknown_session_is_refused(conn, capsys):
    assert cli.main(["cron", "add", "--session=ghost", "--cron", "* * * * *", "--", "go"]) == 1
    assert "no such session" in capsys.readouterr().err
