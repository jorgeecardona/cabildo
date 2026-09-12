"""Machine-wide conditions: the nudge channel for state no agent can see from
inside its own turn.

The contract being pinned here is delivery discipline, because that is what
decides whether the mechanism is affordable enough to leave on:

  * a condition reaches sessions that started after it was raised (a message
    cannot — channel unread begins at join);
  * it is announced once per episode, however many tool calls go by;
  * changing level is news; persisting at the same level is not;
  * recovery is announced only to whoever heard the alarm;
  * a producer that dies takes its alarm with it.
"""

from __future__ import annotations

import pytest

from cabildo import monitor, notify, procs, store
from tests.conftest import make_session


def _raise(conn, **kw):
    kw.setdefault("key", "mem")
    kw.setdefault("level", "warn")
    kw.setdefault("headline", "12% RAM available")
    return store.raise_condition(conn, host="box", **kw)


def _undelivered(conn, sid):
    return store.undelivered_conditions(conn, sid, host="box")


def _deliver(conn, sid):
    rows = _undelivered(conn, sid)
    store.mark_conditions_delivered(conn, sid, rows)
    return rows


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def test_delivered_once_per_episode(conn):
    make_session(conn, "a" * 8)
    _raise(conn)
    assert len(_deliver(conn, "a" * 8)) == 1
    # Same condition, refreshed by the producer every sweep: silence.
    for _ in range(5):
        _raise(conn)
        assert _undelivered(conn, "a" * 8) == []


def test_reaches_a_session_that_did_not_exist_yet(conn):
    """The whole reason this isn't a message. Channel unread starts at join, so
    a message about memory is invisible to the session most likely to make it
    worse: the one that just started.
    """
    _raise(conn)
    make_session(conn, "late", claude_pid=999)
    assert [c["key"] for c in _undelivered(conn, "late")] == ["mem"]


def test_escalation_is_news_in_both_directions(conn):
    make_session(conn, "a" * 8)
    _raise(conn, level="warn")
    _deliver(conn, "a" * 8)

    _raise(conn, level="critical", headline="4% RAM available")
    assert len(_deliver(conn, "a" * 8)) == 1

    # An agent told to stop starting builds is owed the downgrade too.
    _raise(conn, level="warn", headline="12% RAM available")
    assert len(_deliver(conn, "a" * 8)) == 1


def test_clear_reaches_only_those_who_heard_the_alarm(conn):
    make_session(conn, "heard")
    _raise(conn)
    _deliver(conn, "heard")

    make_session(conn, "newcomer", claude_pid=2)
    store.clear_condition(conn, key="mem", host="box", headline="memory recovered")

    assert [c["state"] for c in _undelivered(conn, "heard")] == ["cleared"]
    assert _undelivered(conn, "newcomer") == []


def test_reraise_after_clear_is_a_new_episode(conn):
    make_session(conn, "a" * 8)
    _raise(conn)
    _deliver(conn, "a" * 8)
    store.clear_condition(conn, key="mem", host="box")
    _deliver(conn, "a" * 8)  # the all-clear

    _raise(conn)
    assert [c["state"] for c in _undelivered(conn, "a" * 8)] == ["raised"]


def test_expired_condition_stops_being_injected(conn):
    """A dead producer must not leave an alarm ringing. Expiry is silent: we
    stopped looking, which is not evidence the machine recovered.
    """
    make_session(conn, "a" * 8)
    _raise(conn, ttl_seconds=-1)
    assert _undelivered(conn, "a" * 8) == []
    assert store.active_conditions(conn, host="box") == []


def test_refresh_after_a_long_blackout_re_announces(conn):
    """Same level, but the row went stale — the daemon was down. We know nothing
    about the gap, so the honest move is to say it again.
    """
    make_session(conn, "a" * 8)
    first = _raise(conn)
    assert len(_deliver(conn, "a" * 8)) == 1  # receipt at episode 1

    def age_by(seconds: float) -> None:
        conn.execute("UPDATE conditions SET expires_at=expires_at-? WHERE key='mem'", (seconds,))
        conn.commit()

    # A refresh that merely arrives late is the same episode continuing.
    age_by(store.CONDITION_TTL + store._LOST_AFTER / 2)
    assert _raise(conn)["episode"] == first["episode"]
    assert _undelivered(conn, "a" * 8) == []

    # Dark for longer than the grace: we know nothing about the gap.
    age_by(store.CONDITION_TTL + store._LOST_AFTER + 10)
    assert _raise(conn)["episode"] == first["episode"] + 1
    assert len(_undelivered(conn, "a" * 8)) == 1


def test_prune_drops_expired_rows_and_their_receipts(conn):
    make_session(conn, "a" * 8)
    _raise(conn)
    _deliver(conn, "a" * 8)
    _raise(conn, ttl_seconds=-1)
    assert store.prune_conditions(conn) == 1
    left = conn.execute("SELECT COUNT(*) n FROM condition_deliveries").fetchone()["n"]
    assert left == 0


def test_hosts_do_not_hear_each_others_alarms(conn):
    """The bus is a shared filesystem; one laptop's memory is not news on
    another.
    """
    make_session(conn, "a" * 8)
    store.raise_condition(conn, host="other-box", key="mem", headline="4% RAM")
    assert _undelivered(conn, "a" * 8) == []


# --------------------------------------------------------------------------- #
# The injected text
# --------------------------------------------------------------------------- #
def test_injection_is_silent_when_nothing_is_raised(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    make_session(conn, "a" * 8)
    assert notify.condition_text(conn, "a" * 8) is None


def test_injection_names_the_agent_actually_holding_the_memory(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    make_session(conn, "hog")
    make_session(conn, "bystander", claude_pid=2)
    _raise(
        conn,
        level="critical",
        advice="stop starting things",
        data={
            "by_session": {
                "hog": {
                    "rss_mb": 8192,
                    "count": 2,
                    "procs": [{"pid": 1, "rss_mb": 8000, "what": "pytest -q"}],
                }
            },
        },
    )

    mine = notify.condition_text(conn, "hog")
    assert mine is not None
    assert "YOU are holding 8.0G" in mine and "pytest -q" in mine
    theirs = notify.condition_text(conn, "bystander")
    assert theirs is not None
    assert "4% RAM" not in theirs or "YOU are holding" not in theirs
    assert "YOU are holding" not in theirs

    # And it does not repeat on the next tool call.
    assert notify.condition_text(conn, "hog") is None


def test_injection_marks_delivered_only_when_asked(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    make_session(conn, "a" * 8)
    _raise(conn)
    assert notify.condition_text(conn, "a" * 8, mark=False) is not None
    assert notify.condition_text(conn, "a" * 8) is not None  # still pending
    assert notify.condition_text(conn, "a" * 8) is None  # now it isn't


# --------------------------------------------------------------------------- #
# Thresholds and hysteresis
# --------------------------------------------------------------------------- #
def _snap(
    avail_pct: float, *, total_gb: float = 32.0, stall: float = 0.0, procs_=()
) -> procs.Snapshot:
    total = int(total_gb * 1048576)
    return procs.Snapshot(
        memory=procs.Memory(total_kb=total, available_kb=int(total * avail_pct / 100)),
        pressure={"full60": stall} if stall else {},
        procs=list(procs_),
    )


TH = monitor.Thresholds()


@pytest.mark.parametrize("avail,expected", [(40, None), (12, "warn"), (3, "critical")])
def test_level_follows_available_memory(avail, expected):
    assert monitor.level_for(_snap(avail), TH) == expected


def test_recovery_must_clear_the_threshold_by_a_margin(conn):
    """Without this a box hovering at the line flaps warn/clear/warn every
    sweep, and every flap costs every session an injection.
    """
    just_over = TH.warn_pct + 1  # 16% — over the raw threshold
    assert monitor.level_for(_snap(just_over), TH) is None
    assert monitor.level_for(_snap(just_over), TH, sticky="warn") == "warn"
    assert monitor.level_for(_snap(TH.warn_pct + TH.clear_margin + 1), TH, sticky="warn") is None


def test_stalling_outranks_a_comfortable_percentage():
    """PSI is the machine actually suffering; the percentage is a proxy for it."""
    assert monitor.level_for(_snap(50, stall=30), TH) == "critical"
    assert monitor.level_for(_snap(50, stall=12), TH) == "warn"


def test_missing_psi_reads_as_no_evidence_not_as_bad_news():
    assert _snap(50).stall_pct == 0.0
    assert monitor.level_for(_snap(50), TH) is None


def test_no_meminfo_says_nothing(monkeypatch):
    """A box that won't tell us about its memory gets no alarm invented for it."""
    empty = procs.Snapshot(memory=procs.Memory())
    assert monitor.level_for(empty, TH) is None


# --------------------------------------------------------------------------- #
# Loose processes
# --------------------------------------------------------------------------- #
def _proc(pid, rss_mb, *, session_id=None, claude_pid=0, via="", cmd="python x.py"):
    return procs.Proc(
        pid=pid,
        ppid=1,
        comm="python",
        rss_kb=rss_mb * 1024,
        started_at=0.0,
        cmdline=cmd,
        session_id=session_id,
        claude_pid=claude_pid,
        via=via,
    )


def test_loose_is_a_process_whose_agent_is_gone():
    live = {"alive"}
    snap = _snap(
        50,
        procs_=[
            _proc(1, 900, session_id="alive", via="path"),  # its agent is working
            _proc(2, 900, session_id="ended", via="path"),  # nobody is waiting
        ],
    )
    assert [p.pid for p in procs.loose(snap, live)] == [2]


def test_unattributable_processes_are_never_called_loose():
    """One nudge about somebody's browser costs more trust than ten correct ones
    earn, so anything we cannot tie to an agent is left alone.
    """
    snap = _snap(50, procs_=[_proc(9, 4000, cmd="/usr/lib/chrome")])
    assert procs.loose(snap, set()) == []


def test_ancestry_attribution_defers_to_a_live_claude():
    snap = _snap(
        50,
        procs_=[
            _proc(3, 900, claude_pid=100, via="ancestry"),
            _proc(4, 900, claude_pid=200, via="ancestry"),
        ],
    )
    loose = procs.loose(snap, set(), alive=lambda pid: pid == 100)
    assert [p.pid for p in loose] == [4]


def test_orphans_stay_quiet_below_the_total_floor(conn, monkeypatch):
    """One stray 300 MB process is life, not an incident."""
    monkeypatch.setattr(store, "hostname", lambda: "box")
    snap = _snap(50, procs_=[_proc(2, 300, session_id="ended", via="path")])
    assert monitor._reconcile_orphans(conn, snap, TH, "test", set()) == []
    assert store.active_conditions(conn, host="box") == []


def test_orphans_raise_and_then_clear_themselves(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    snap = _snap(50, procs_=[_proc(2, 2000, session_id="ended", via="path")])
    assert monitor._reconcile_orphans(conn, snap, TH, "test", set())
    row = store.active_conditions(conn, host="box")[0]
    assert row["key"] == "orphans" and row["data"]["procs"][0]["pid"] == 2

    monitor._reconcile_orphans(conn, _snap(50), TH, "test", set())
    assert store.active_conditions(conn, host="box") == []
