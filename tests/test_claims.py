"""Advisory-lease coordination: acquire, conflict, refresh, expiry, release,
and cleanup on session end."""

from __future__ import annotations

import pytest

from backchat import store
from tests.conftest import make_session


@pytest.fixture()
def clock(monkeypatch):
    """Controllable time source so TTL expiry is deterministic."""
    state = {"t": 1_000.0}
    monkeypatch.setattr(store.time, "time", lambda: state["t"])
    return state


def _acquire(conn, resource, sid, ttl=600, note=None):
    return store.acquire_claim(
        conn, resource=resource, holder_session=sid,
        holder_handle=None, project="/repo/alpha", ttl_seconds=ttl, note=note,
    )


def test_first_acquire_succeeds(conn, clock):
    make_session(conn, "a", handle="alice")
    res = _acquire(conn, "push:alpha", "a", note="rebasing")
    assert res["ok"] is True
    assert store.list_claims(conn, project="/repo/alpha")[0]["resource"] == "push:alpha"


def test_second_holder_is_blocked_and_told_who(conn, clock):
    make_session(conn, "a", handle="alice")
    make_session(conn, "b", handle="bob")
    _acquire(conn, "push:alpha", "a", note="rebasing")
    res = _acquire(conn, "push:alpha", "b")
    assert res["ok"] is False
    assert res["held_by"] == "@alice"
    assert res["note"] == "rebasing"
    assert res["expires_in"] > 0


def test_same_holder_refreshes(conn, clock):
    make_session(conn, "a", handle="alice")
    _acquire(conn, "push:alpha", "a", ttl=100)
    clock["t"] += 50
    res = _acquire(conn, "push:alpha", "a", ttl=100)  # refresh before expiry
    assert res["ok"] is True
    # only one claim row for the resource
    claims = store.list_claims(conn)
    assert len([c for c in claims if c["resource"] == "push:alpha"]) == 1


def test_expired_lock_can_be_taken_by_another(conn, clock):
    make_session(conn, "a", handle="alice")
    make_session(conn, "b", handle="bob")
    _acquire(conn, "push:alpha", "a", ttl=100)
    clock["t"] += 101  # a's lease has expired
    res = _acquire(conn, "push:alpha", "b")
    assert res["ok"] is True
    # now held by b
    active = store.list_claims(conn)
    assert active[0]["holder"] == "@bob"


def test_expired_lock_not_listed_as_active(conn, clock):
    make_session(conn, "a", handle="alice")
    _acquire(conn, "push:alpha", "a", ttl=100)
    clock["t"] += 200
    assert store.list_claims(conn) == []


def test_release_only_by_holder(conn, clock):
    make_session(conn, "a", handle="alice")
    make_session(conn, "b", handle="bob")
    _acquire(conn, "push:alpha", "a")
    assert store.release_claim(conn, resource="push:alpha", holder_session="b") is False
    assert store.release_claim(conn, resource="push:alpha", holder_session="a") is True
    assert store.list_claims(conn) == []


def test_end_session_drops_its_locks(conn, clock):
    make_session(conn, "a", handle="alice")
    _acquire(conn, "push:alpha", "a")
    _acquire(conn, "file:x.py", "a")
    store.end_session(conn, "a")
    assert store.list_claims(conn) == []
    assert store.get_session(conn, "a")["status"] == "ended"


def test_claims_scoped_by_project(conn, clock):
    make_session(conn, "a", handle="a", project="/repo/alpha", project_name="alpha")
    make_session(conn, "b", handle="b", project="/repo/beta", project_name="beta")
    store.acquire_claim(conn, resource="push:alpha", holder_session="a",
                        holder_handle="a", project="/repo/alpha", ttl_seconds=600, note=None)
    store.acquire_claim(conn, resource="push:beta", holder_session="b",
                        holder_handle="b", project="/repo/beta", ttl_seconds=600, note=None)
    alpha = store.list_claims(conn, project="/repo/alpha")
    assert {c["resource"] for c in alpha} == {"push:alpha"}
