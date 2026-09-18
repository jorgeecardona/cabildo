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


def test_localbus_unknown_op_raises(home):
    with pytest.raises(KeyError):
        LocalBus().call("nope", {}, identity=Identity(session="x"))


def test_localbus_reads_return_json_safe_shapes(home):
    bus = LocalBus()
    ident = Identity(session="x")
    assert isinstance(bus.call("list_topics", {}, identity=ident), list)
    assert isinstance(bus.call("claims", {}, identity=ident), list)
    assert isinstance(bus.call("list_facts", {}, identity=ident), list)
