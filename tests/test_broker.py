"""The socket broker + SocketBus: a client over the wire gets the same results as
LocalBus, and a bad call comes back as a BusError rather than taking the server down.

Phase 2 of the broker refactor (docs/design/broker-service.md). The server runs on
a thread against the test's throwaway DB; SocketBus is the client.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest

from cabildo import broker, store
from cabildo.bus import BusError, Identity, LocalBus, SocketBus
from tests.conftest import make_session


@pytest.fixture
def running_broker(home):
    # AF_UNIX caps the socket path near 104 chars and pytest's tmp_path is often
    # longer, so bind under /tmp instead of home.
    addr = f"/tmp/cabildo-test-{uuid.uuid4().hex[:8]}.sock"  # noqa: S108
    stop = threading.Event()
    ready = threading.Event()
    t = threading.Thread(
        target=broker.serve,
        kwargs={"address": addr, "ready": ready, "stop": stop},
        daemon=True,
    )
    t.start()
    assert ready.wait(timeout=5), "broker did not start"
    yield addr
    stop.set()
    t.join(timeout=5)
    Path(addr).unlink(missing_ok=True)


def test_socketbus_matches_localbus(running_broker):
    c = store.connect()
    make_session(c, "alice", handle="alice")
    make_session(c, "bob", handle="bob")
    c.close()

    sock = SocketBus(running_broker)
    alice = Identity(session="alice", handle="alice")
    bob = Identity(session="bob", handle="bob")

    res = sock.call("send", {"to_session": "bob", "body": "over the wire"}, identity=alice)
    assert res["id"] > 0

    box = sock.call("inbox", {}, identity=bob)
    assert [m["body"] for m in box] == ["over the wire"]

    # Parity with the in-process backend against the same DB.
    over_wire = {a["session_id"] for a in sock.call("list_agents", {}, identity=alice)}
    in_process = {a["session_id"] for a in LocalBus().call("list_agents", {}, identity=alice)}
    assert over_wire == in_process == {"alice", "bob"}


def test_socketbus_unknown_op_raises_buserror(running_broker):
    with pytest.raises(BusError) as ei:
        SocketBus(running_broker).call("nope", {}, identity=Identity(session="x"))
    assert ei.value.kind == "unknown_op"


def test_socketbus_handler_error_is_reported_and_server_survives(running_broker):
    # get_message with no message_id -> the handler raises KeyError, which comes
    # back as a BusError instead of crashing the broker.
    with pytest.raises(BusError):
        SocketBus(running_broker).call("get_message", {}, identity=Identity(session="x"))
    # The server is still serving afterwards.
    topics = SocketBus(running_broker).call("list_topics", {}, identity=Identity(session="x"))
    assert isinstance(topics, list)
