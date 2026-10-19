"""LocalBus: the direct-SQLite backend behind the Bus seam behaves like the store.

Phase 1 of the broker refactor (docs/design/broker-service.md). Nothing routes
through the bus yet, so these tests pin the seam's behaviour directly: an op maps
to the same result the store would give, identity flows in via the envelope, and
an unknown op is a clean error rather than a silent miss.
"""

from __future__ import annotations

import pytest

from cabildo import store
from cabildo.bus import Identity, LocalBus
from tests.conftest import make_session


def test_localbus_list_agents_send_and_inbox(home):
    c = store.connect()
    make_session(c, "alice", handle="alice")
    make_session(c, "bob", handle="bob")
    c.close()

    bus = LocalBus()

    agents = bus.call("list_agents", {}, identity=Identity(session="alice"))
    ids = {a["session_id"] for a in agents}
    assert {"alice", "bob"} <= ids
    assert all("presence" in a and "display" in a for a in agents)

    res = bus.call(
        "send",
        {"to_session": "bob", "body": "hi bob"},
        identity=Identity(session="alice", handle="alice"),
    )
    assert res["id"] > 0

    box = bus.call("inbox", {}, identity=Identity(session="bob"))
    assert [m["body"] for m in box] == ["hi bob"]

    full = bus.call("get_message", {"message_id": box[0]["id"]}, identity=Identity(session="bob"))
    assert full is not None
    assert full["body"] == "hi bob"


def test_localbus_topic_subscribe_send_and_mark_read(home):
    c = store.connect()
    make_session(c, "alice", handle="alice")
    make_session(c, "bob", handle="bob")
    c.close()

    bus = LocalBus()
    alice = Identity(session="alice", handle="alice")
    bob = Identity(session="bob", handle="bob")

    # bob must be subscribed before the message is posted (the joined_at bound).
    assert bus.call("join_topic", {"topic": "general"}, identity=bob)["ok"]
    bus.call("send", {"topic": "general", "body": "hello all"}, identity=alice)

    box = bus.call("inbox", {}, identity=bob)
    assert [m["body"] for m in box] == ["hello all"]

    assert bus.call("mark_read", {"topic": "general"}, identity=bob) == {"marked": 1}
    assert bus.call("inbox", {}, identity=bob) == []

    # After leaving, a later post to the channel is not in bob's inbox.
    bus.call("leave_topic", {"topic": "general"}, identity=bob)
    bus.call("send", {"topic": "general", "body": "after leave"}, identity=alice)
    assert bus.call("inbox", {}, identity=bob) == []


def test_localbus_claim_release_is_exclusive(home):
    c = store.connect()
    make_session(c, "alice", handle="alice")
    make_session(c, "bob", handle="bob")
    c.close()

    bus = LocalBus()
    alice = Identity(session="alice", handle="alice")
    bob = Identity(session="bob", handle="bob")

    assert bus.call("claim", {"resource": "push:repo"}, identity=alice)["ok"] is True
    held = bus.call("claim", {"resource": "push:repo"}, identity=bob)
    assert held["ok"] is False
    assert held["held_by_session"] == "alice"
    assert bus.call("release", {"resource": "push:repo"}, identity=alice) == {
        "ok": True,
        "resource": "push:repo",
    }
    assert bus.call("claim", {"resource": "push:repo"}, identity=bob)["ok"] is True


def test_localbus_set_handle_and_request(home):
    c = store.connect()
    make_session(c, "alice")
    c.close()

    bus = LocalBus()
    alice = Identity(session="alice", project="/repo/alpha")

    assert bus.call("set_handle", {"handle": "ace"}, identity=alice) == {
        "ok": True,
        "handle": "ace",
    }

    ok = bus.call("request", {"action": "push", "params": {"cwd": "/repo/alpha"}}, identity=alice)
    assert ok["ok"] is True
    assert ok["intent_id"] > 0

    bad = bus.call("request", {"action": "nope"}, identity=alice)
    assert bad["ok"] is False


def test_localbus_unknown_op_raises(home):
    with pytest.raises(KeyError):
        LocalBus().call("nope", {}, identity=Identity(session="x"))


def test_localbus_reads_return_json_safe_shapes(home):
    bus = LocalBus()
    ident = Identity(session="x")
    assert isinstance(bus.call("list_topics", {}, identity=ident), list)
    assert isinstance(bus.call("claims", {}, identity=ident), list)
    assert isinstance(bus.call("list_decisions", {}, identity=ident), list)
