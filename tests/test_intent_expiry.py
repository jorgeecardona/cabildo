"""A queued intent is a statement about a moment, and moments expire.

This came out of live behaviour: the first push-enabled daemon came up and
immediately drained a `land` intent that had been sitting in the queue for 190
hours. Two independent guards happened to catch it (dirty tree, rejected push),
but the queue itself had no opinion about age — so on a repo where the push would
have succeeded, the daemon would have landed week-old work onto a branch that had
moved on, for an agent that no longer existed, with nobody watching the result.
"""

from __future__ import annotations

import time

from cabildo import actions, daemon, store

from .conftest import make_session


def queue(conn, *, age_h: float, action: str = "land", session: str = "s1") -> int:
    """An intent that was queued `age_h` hours ago."""
    make_session(conn, session, handle="worker")
    intent_id = store.request_intent(
        conn,
        from_session=session,
        from_handle="worker",
        project=None,
        action=action,
        params={"cwd": "/nope", "target": "main"},
    )
    conn.execute(
        "UPDATE intents SET created_at=? WHERE id=?", (time.time() - age_h * 3600, intent_id)
    )
    conn.commit()
    return intent_id


def ctx(**kw) -> actions.Ctx:
    return actions.Ctx(worker="test", log=lambda _m: None, **kw)


def drain(conn, c: actions.Ctx) -> None:
    """One daemon pass: take whatever is queued and handle it."""
    while (intent := store.take_next_intent(conn, "test")) is not None:
        daemon.handle(conn, intent, c)


def test_dry_run_land_reports_dry_run_not_done(conn, monkeypatch):
    # Issue #15: a dry-run land (ok, but nothing pushed) must not land as `done`,
    # and the requester's DM must not claim a push happened.
    monkeypatch.setitem(
        actions.ACTIONS,
        "land",
        lambda _p, _c: {
            "ok": True,
            "landed": False,
            "status": "dry_run",
            "gate": "ABSENT",
            "reason": "dry run — nothing pushed",
        },
    )
    queue(conn, age_h=0)
    drain(conn, ctx(max_intent_age=0))

    row = store.list_intents(conn)[0]
    assert row["status"] == "dry_run"  # not "done"

    body = store.inbox(conn, "s1")[-1]["body"]
    assert "pushed on your word alone" not in body  # nothing was pushed
    assert "dry run" in body.lower()


def test_a_stale_intent_is_refused_not_run(conn, monkeypatch):
    called = []
    monkeypatch.setitem(actions.ACTIONS, "land", lambda p, c: called.append(p) or {"ok": True})
    queue(conn, age_h=190)

    drain(conn, ctx(max_intent_age=2 * 3600))

    assert called == [], "the action ran on a 190h-old request"
    row = store.list_intents(conn)[0]
    assert row["status"] == "expired"
    assert row["result"]["expired"] is True
    assert "190" in row["result"]["reason"]  # says how old, so it's arguable
    assert "ceiling" in row["result"]["reason"]


def test_a_fresh_intent_still_runs(conn, monkeypatch):
    called = []
    monkeypatch.setitem(actions.ACTIONS, "land", lambda p, c: called.append(p) or {"ok": True})
    queue(conn, age_h=0.5)

    drain(conn, ctx(max_intent_age=2 * 3600))

    assert len(called) == 1
    assert store.list_intents(conn)[0]["status"] == "done"


def test_the_ceiling_can_be_switched_off(conn, monkeypatch):
    called = []
    monkeypatch.setitem(actions.ACTIONS, "land", lambda p, c: called.append(p) or {"ok": True})
    queue(conn, age_h=500)

    drain(conn, ctx(max_intent_age=0))

    assert len(called) == 1, "0 must mean 'no ceiling', not 'expire everything'"


def test_expiry_is_reported_to_the_requester_but_not_urgent(conn):
    """A failed land blocks its agent, so it earns an urgent DM — which also pops
    a desktop notification for the human. A week-old request nobody is waiting on
    must not buy itself a popup.
    """
    queue(conn, age_h=100, session="s1")

    drain(conn, ctx(max_intent_age=2 * 3600))

    msgs = [m for m in store.history(conn, limit=10) if m["from_session"] == daemon.DAEMON_SESSION]
    assert len(msgs) == 1
    assert msgs[0]["to_session"] == "s1"
    assert msgs[0]["urgent"] == 0
    assert "expired" in msgs[0]["body"]


def test_failures_are_still_urgent(conn, monkeypatch):
    """The change to urgency must not have quietly muted real failures."""
    monkeypatch.setitem(
        actions.ACTIONS, "land", lambda p, c: {"ok": False, "reason": "push rejected"}
    )
    queue(conn, age_h=0.1, session="s1")

    drain(conn, ctx(max_intent_age=2 * 3600))

    msgs = [m for m in store.history(conn, limit=10) if m["from_session"] == daemon.DAEMON_SESSION]
    assert msgs[0]["urgent"] == 1


def test_count_pending_intents_splits_the_backlog(conn):
    queue(conn, age_h=190, session="s1")
    queue(conn, age_h=0.1, session="s2")

    assert store.count_pending_intents(conn) == 2
    assert store.count_pending_intents(conn, older_than=2 * 3600) == 1
    assert store.count_pending_intents(conn, older_than=1000 * 3600) == 0


def test_an_expired_intent_leaves_the_queue(conn):
    """It must not come back on the next poll: a queue that never drains is
    exactly the failure this replaced.
    """
    queue(conn, age_h=190)
    c = ctx(max_intent_age=2 * 3600)
    drain(conn, c)
    assert store.count_pending_intents(conn) == 0
    drain(conn, c)  # second pass finds nothing
    assert store.count_pending_intents(conn) == 0
