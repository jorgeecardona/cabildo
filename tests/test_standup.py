"""Standup: a read-only digest of who touched what — messages, tasks in flight, and
active agents — from cabildo's own record.
"""

from __future__ import annotations

from cabildo import standup, store
from tests.conftest import make_session


def _msg(conn, sid, handle, body):
    store.post_message(
        conn,
        from_session=sid,
        from_handle=handle,
        body=body,
        topic="general",
        to_session=None,
        urgent=False,
    )


def test_collect_counts_messages_tasks_and_agents(conn):
    make_session(conn, "a", handle="alice")
    make_session(conn, "b", handle="bob")
    _msg(conn, "a", "alice", "hi")
    _msg(conn, "a", "alice", "again")
    store.acquire_claim(
        conn,
        resource="issue:some-task",
        holder_session="b",
        holder_handle="bob",
        project=None,
        ttl_seconds=600,
        note=None,
    )
    d = standup.collect(conn, hours=24)
    assert d["messages"]["total"] == 2
    assert d["messages"]["by_sender"]["@alice"] == 2
    assert d["tasks_in_flight"] == {"some-task": "@bob"}
    assert {"@alice", "@bob"} <= set(d["agents_active"])

    text = standup.render(d)
    assert "standup" in text
    assert "messages: 2" in text
    assert "some-task" in text and "@bob" in text


def test_collect_is_empty_on_a_quiet_bus(conn):
    d = standup.collect(conn, hours=24)
    assert d["messages"]["total"] == 0
    assert d["tasks_in_flight"] == {}
    assert "none claimed" in standup.render(d)
