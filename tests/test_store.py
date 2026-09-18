"""Core data-layer behavior: sessions, topics, messaging, unread + explicit
read semantics, DMs, and the PID rendezvous lookup.
"""

from __future__ import annotations

from cabildo import store
from tests.conftest import make_session


def test_spawn_index_maps_registered_sub_agents(conn):
    store.record_spawn(
        conn,
        tag="spawn-qa-0101-99",
        parent_session="parent-sid",
        role="qa",
        task="verify the gate is trustworthy",
        depth=1,
    )
    # Recorded but the child has not registered yet -> not indexed (no session_id).
    assert store.spawn_index(conn) == {}
    # The child registers under its tag; register_session backfills the session_id.
    store.register_session(conn, "child-sid", tag="spawn-qa-0101-99")
    idx = store.spawn_index(conn)
    assert idx["child-sid"]["role"] == "qa"
    assert idx["child-sid"]["parent_session"] == "parent-sid"
    assert idx["child-sid"]["status"] == "registered"


def test_register_creates_default_subscriptions(conn):
    make_session(conn, "s1", project_name="alpha")
    subs = set(store.subscriptions_of(conn, "s1"))
    # Isolation by default: an agent's only default home is its own project
    # channel. #general exists but is opt-in via join_topic.
    assert subs == {store.project_topic("alpha")}
    assert store.DEFAULT_TOPIC in {t["name"] for t in store.list_topics(conn)}


def test_general_is_opt_in(conn):
    make_session(conn, "s1", project_name="alpha")
    store.subscribe(conn, "s1", store.DEFAULT_TOPIC)
    assert store.DEFAULT_TOPIC in set(store.subscriptions_of(conn, "s1"))


def test_pid_rendezvous_lookup(conn):
    make_session(conn, "s1", claude_pid=4242)
    assert store.session_for_pid(conn, 4242) == "s1"
    assert store.session_for_pid(conn, 9999) is None
    assert store.session_for_pid(conn, 0) is None


def test_topic_message_is_unread_for_subscribers_not_sender(conn):
    make_session(conn, "sender", handle="snd", project_name="alpha")
    make_session(conn, "recv", project_name="alpha")  # same project -> same topic
    topic = store.project_topic("alpha")

    store.post_message(
        conn, from_session="sender", from_handle="snd", body="hello team", topic=topic
    )

    # recipient sees it unread; sender does not see own message
    assert len(store.inbox(conn, "recv", unread_only=True)) == 1
    assert len(store.inbox(conn, "sender", unread_only=True)) == 0
    assert store.unread_summary(conn, "recv")["count"] == 1


def test_messages_are_not_auto_read_only_explicit(conn):
    make_session(conn, "a", handle="a", project_name="alpha")
    make_session(conn, "b", project_name="alpha")
    topic = store.project_topic("alpha")
    store.post_message(conn, from_session="a", from_handle="a", body="m1", topic=topic)

    # merely reading the inbox does NOT mark it read
    store.inbox(conn, "b", unread_only=True)
    assert store.unread_summary(conn, "b")["count"] == 1

    # explicit read clears it
    marked = store.mark_read(conn, "b", all_unread=True)
    assert marked == 1
    assert store.unread_summary(conn, "b")["count"] == 0
    # idempotent: re-marking marks nothing new
    assert store.mark_read(conn, "b", all_unread=True) == 0


def test_direct_message_only_reaches_target(conn):
    make_session(conn, "a", handle="alice")
    make_session(conn, "b", handle="bob")
    make_session(conn, "c", handle="carol")
    store.post_message(conn, from_session="a", from_handle="alice", body="psst bob", to_session="b")

    assert store.unread_summary(conn, "b")["count"] == 1
    assert store.unread_summary(conn, "c")["count"] == 0
    inbox_b = store.inbox(conn, "b")
    assert inbox_b[0]["to_session"] == "b"
    assert inbox_b[0]["from_display"] == "@alice"


def test_mark_read_by_specific_ids_and_by_topic(conn):
    make_session(conn, "a", handle="a", project_name="alpha")
    make_session(conn, "b", project_name="alpha")
    topic = store.project_topic("alpha")
    m1 = store.post_message(conn, from_session="a", from_handle="a", body="1", topic=topic)
    store.post_message(conn, from_session="a", from_handle="a", body="2", topic=topic)

    assert store.mark_read(conn, "b", message_ids=[m1]) == 1
    assert store.unread_summary(conn, "b")["count"] == 1
    assert store.mark_read(conn, "b", topic=topic) == 1
    assert store.unread_summary(conn, "b")["count"] == 0


def test_post_message_requires_exactly_one_destination(conn):
    make_session(conn, "a", handle="a")
    import pytest

    with pytest.raises(ValueError):
        store.post_message(conn, from_session="a", from_handle="a", body="x")
    with pytest.raises(ValueError):
        store.post_message(
            conn, from_session="a", from_handle="a", body="x", topic="t", to_session="a"
        )


def test_active_sessions_scoped_by_project_and_excludes_self(conn):
    make_session(conn, "a", handle="a", project="/repo/alpha", project_name="alpha")
    make_session(conn, "b", handle="b", project="/repo/alpha", project_name="alpha")
    make_session(conn, "c", handle="c", project="/repo/beta", project_name="beta")

    peers = store.active_sessions(conn, project="/repo/alpha", exclude="a")
    assert {p["session_id"] for p in peers} == {"b"}
    everyone = store.active_sessions(conn, exclude="a")
    assert {p["session_id"] for p in everyone} == {"b", "c"}


def test_history_returns_thread_regardless_of_read_state(conn):
    make_session(conn, "a", handle="a")
    make_session(conn, "b", handle="b")
    store.post_message(conn, from_session="a", from_handle="a", body="hi", to_session="b")
    store.post_message(conn, from_session="b", from_handle="b", body="yo", to_session="a")
    thread = store.history(conn, between=("a", "b"))
    assert [m["body"] for m in thread] == ["hi", "yo"]


def test_display_name_prefers_handle(conn):
    make_session(conn, "abcd1234ef", handle="cooldev")
    row = store.get_session(conn, "abcd1234ef")
    assert store.display_name(row) == "@cooldev"
    store.set_handle(conn, "abcd1234ef", "")  # cleared
    row = store.get_session(conn, "abcd1234ef")
    # falsy handle -> deterministic robot codename
    from cabildo import names

    assert store.display_name(row) == names.robot_name("abcd1234ef")


def test_robot_name_is_deterministic_and_id_like():
    from cabildo import names

    sid = "8f801073-440e-4c02-956f-511a8704a7bc"
    name = names.robot_name(sid)
    # stable: same id -> same name every time
    assert name == names.robot_name(sid)
    # shaped like <adjective>-<noun>-<hhhhh>, tail carries the id's leading hex
    adj, noun, tail = name.split("-")
    assert adj in names.ADJECTIVES
    assert noun in names.NOUNS
    assert tail == "8f801"
    # distinct ids generally get distinct names
    assert names.robot_name("f6200761-0000-0000-0000-000000000000") != name
    # empty / unknown collapses to "?"
    assert names.robot_name("") == "?"
    assert names.robot_name(None) == "?"


def test_robot_name_resolves_as_address(conn):
    from cabildo import names

    sid = "8f801073-440e-4c02-956f-511a8704a7bc"
    make_session(conn, sid)
    name = names.robot_name(sid)
    assert store.session_for_robot_name(conn, name) == sid
    assert store.session_for_robot_name(conn, name.lower()) == sid  # case-insensitive
    assert store.session_for_robot_name(conn, "@" + name) == sid  # @-prefixed
    assert store.session_for_robot_name(conn, "NOPE-00") is None


def test_presence_state_derives_from_last_seen(conn):
    make_session(conn, "a", project_name="alpha")
    row = store.get_session(conn, "a")
    assert row is not None
    base = row["last_seen_at"]
    assert store.presence_state(row, base + 5) == "working"
    assert store.presence_state(row, base + store.WORKING_WINDOW + 1) == "idle"
    assert store.presence_state(row, base + store.ACTIVE_WINDOW + 1) == "stale"
    assert store.presence_state(None) == "unknown"

    conn.execute("UPDATE sessions SET status='ended' WHERE session_id='a'")
    assert store.presence_state(store.get_session(conn, "a"), base + 5) == "ended"
