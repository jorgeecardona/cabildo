"""The socket broker + SocketBus: a client over the wire gets the same results as
LocalBus, and a bad call comes back as a BusError rather than taking the server down.

Phase 2 of the broker refactor (docs/design/broker-service.md). The server runs on
a thread against the test's throwaway DB; SocketBus is the client.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest

from cabildo import broker, bus, daemon, store
from cabildo.bus import BusError, Identity, LocalBus, SocketBus
from tests.conftest import make_session


def _short_socket() -> str:
    # AF_UNIX caps the path near 104 chars; pytest's tmp_path is often longer.
    return f"/tmp/cabildo-test-{uuid.uuid4().hex[:8]}.sock"  # noqa: S108


@pytest.fixture
def running_broker(home):
    addr = _short_socket()
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


def test_daemon_serves_the_broker(home):
    addr = _short_socket()
    stop = threading.Event()
    t = threading.Thread(
        target=daemon.main,
        args=(
            [
                "--broker",
                "--no-monitor",
                "--no-notify",
                "--interval",
                "0.1",
                "--broker-socket",
                addr,
            ],
        ),
        kwargs={"stop": stop},
        daemon=True,
    )
    t.start()
    try:
        probe = Identity(session="probe")
        deadline = time.time() + 5
        while True:  # wait for the daemon's broker to accept connections
            try:
                topics = SocketBus(addr).call("list_topics", {}, identity=probe)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)
        assert isinstance(topics, list)
        # The daemon registered itself on startup, so it shows up as an agent.
        agents = {a["session_id"] for a in SocketBus(addr).call("list_agents", {}, identity=probe)}
        assert "daemon" in agents
    finally:
        stop.set()
        t.join(timeout=5)
    Path(addr).unlink(missing_ok=True)


def test_default_bus_is_local_without_a_broker(home, monkeypatch):
    monkeypatch.delenv("CABILDO_BROKER", raising=False)
    monkeypatch.delenv("CABILDO_BROKER_SOCKET", raising=False)
    assert isinstance(bus.default_bus(), LocalBus)


def test_default_bus_uses_the_socket_when_a_broker_is_live(running_broker, monkeypatch):
    monkeypatch.delenv("CABILDO_BROKER", raising=False)
    monkeypatch.setenv("CABILDO_BROKER_SOCKET", running_broker)
    assert isinstance(bus.default_bus(), SocketBus)


def test_default_bus_off_switch_forces_local(running_broker, monkeypatch):
    monkeypatch.setenv("CABILDO_BROKER_SOCKET", running_broker)
    monkeypatch.setenv("CABILDO_BROKER", "0")
    assert isinstance(bus.default_bus(), LocalBus)


def test_socket_live_is_false_for_a_stale_socket_file(home, tmp_path):
    stale = str(tmp_path / "stale.sock")
    Path(stale).touch()  # a plain file where a socket used to be: exists but dead
    assert bus._socket_live(stale) is False
