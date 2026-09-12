"""Context-bloat fixes: an inbox must not blow up a session's token budget.

Three data-layer guarantees:
  1. Subscribing to a busy channel does NOT inherit its backlog as unread
     (unread starts at join time).
  2. get_message returns a full body only for messages actually in your inbox.
  3. reap_dead_sessions ends 'active' rows whose owning process is gone.
"""

from __future__ import annotations

import pytest

from cabildo import store
from tests.conftest import make_session


class Clock:
    """Deterministic replacement for store._now, so 'before/after join' is
    exact rather than at the mercy of wall-clock resolution.
    """

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def tick(self, dt: float = 1.0) -> float:
        self.t += dt
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(store, "_now", c.now)
    return c


# --------------------------------------------------------------------------- #
# Fix #1: unread starts at join time
# --------------------------------------------------------------------------- #
def test_channel_backlog_before_join_is_not_unread(conn, clock):
    make_session(conn, "old", handle="old", project_name="alpha")
    topic = store.project_topic("alpha")
    for i in range(5):
        clock.tick()
        store.post_message(
            conn, from_session="old", from_handle="old", body=f"old-{i}", topic=topic
        )

    # A fresh agent joins the busy channel well after the backlog exists.
    clock.tick(100)
    make_session(conn, "new", project_name="alpha")

    # It inherits none of the 5 prior messages.
    assert store.unread_summary(conn, "new")["count"] == 0
    assert store.inbox(conn, "new", unread_only=True) == []

    # But a message posted after it joined IS unread.
    clock.tick()
    store.post_message(conn, from_session="old", from_handle="old", body="fresh", topic=topic)
    assert store.unread_summary(conn, "new")["count"] == 1


def test_message_at_exact_join_instant_is_unread(conn, clock):
    """Boundary: created_at == joined_at counts as unread (>=), so nothing
    posted the same tick you subscribe is silently dropped.
    """
    make_session(conn, "old", handle="old", project_name="alpha")
    topic = store.project_topic("alpha")
    make_session(conn, "new", project_name="alpha")  # joins at clock.now()
    # same instant, no tick
    store.post_message(
        conn, from_session="old", from_handle="old", body="simultaneous", topic=topic
    )
    assert store.unread_summary(conn, "new")["count"] == 1


def test_dm_is_unread_regardless_of_join_time(conn, clock):
    """A direct message is always yours — the join-time bound is only for
    channel backlog, never DMs.
    """
    make_session(conn, "a", handle="a", project_name="alpha")
    clock.tick(50)
    make_session(conn, "b", project_name="beta")
    # DM predates nothing relevant; it targets b directly.
    store.post_message(conn, from_session="a", from_handle="a", body="direct", to_session="b")
    assert store.unread_summary(conn, "b")["count"] == 1


# --------------------------------------------------------------------------- #
# Fix #3: get_message full-body fetch, scoped to the inbox
# --------------------------------------------------------------------------- #
def test_get_message_returns_full_body_for_inbox_message(conn, clock):
    make_session(conn, "a", handle="a", project_name="alpha")
    make_session(conn, "b", project_name="alpha")
    topic = store.project_topic("alpha")
    clock.tick()
    body = "X" * 4000
    mid = store.post_message(conn, from_session="a", from_handle="a", body=body, topic=topic)
    got = store.get_message(conn, "b", mid)
    assert got is not None
    assert got["body"] == body


def test_get_message_denies_messages_not_in_inbox(conn, clock):
    make_session(conn, "a", handle="a", project_name="alpha")
    make_session(conn, "b", project_name="alpha")
    make_session(conn, "c", project_name="beta")  # different project/channel
    topic = store.project_topic("alpha")
    clock.tick()
    mid = store.post_message(conn, from_session="a", from_handle="a", body="secret", topic=topic)
    # Outsider on another channel can't fetch it.
    assert store.get_message(conn, "c", mid) is None
    # Sender doesn't get its own message back through the inbox predicate.
    assert store.get_message(conn, "a", mid) is None
    # Recipient does.
    assert store.get_message(conn, "b", mid) is not None


# --------------------------------------------------------------------------- #
# Fix #2: reap dead sessions
# --------------------------------------------------------------------------- #
def _status(conn, session_id: str) -> str:
    row = store.get_session(conn, session_id)
    assert row is not None
    return row["status"]


def test_reap_ends_only_dead_sessions(conn):
    make_session(conn, "alive", claude_pid=111)
    make_session(conn, "dead", claude_pid=222)
    reaped = store.reap_dead_sessions(conn, is_alive=lambda p: p == 111)
    assert reaped == 1
    assert _status(conn, "dead") == "ended"
    assert _status(conn, "alive") == "active"


def test_reap_skips_sessions_without_pid(conn):
    make_session(conn, "nopid", claude_pid=0)
    reaped = store.reap_dead_sessions(conn, is_alive=lambda p: False)
    assert reaped == 0
    assert _status(conn, "nopid") == "active"


def test_reap_releases_claims_of_dead_session(conn):
    make_session(conn, "dead", handle="d", claude_pid=222)
    store.acquire_claim(
        conn,
        resource="push:alpha",
        holder_session="dead",
        holder_handle="d",
        project="/repo/alpha",
        ttl_seconds=3600,
        note=None,
    )
    assert any(c["resource"] == "push:alpha" for c in store.list_claims(conn))
    store.reap_dead_sessions(conn, is_alive=lambda p: False)
    assert not any(c["resource"] == "push:alpha" for c in store.list_claims(conn))
