"""LAN pairing (cabildo.pair) and paired sync (cabildo.lan.Service).

Machine A runs in a subprocess with its own CABILDO_HOME; machine B is this
test process. Both talk over loopback TLS, so this is the real wire.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cabildo import lan, pair, store

pytestmark = pytest.mark.skipif(not pair.openssl_available(), reason="needs openssl")


@pytest.fixture(autouse=True)
def _bus_exists(home):
    """Create the bus before any service thread opens it: two connections creating a
    brand-new WAL database at the same moment can get 'database is locked'. A real
    daemon always finds the bus already there.
    """
    store.connect().close()


def test_tag_binds_role_and_fingerprints():
    t = pair.tag("123456", "b", "fa", "fb", "na", "nb")
    assert t == pair.tag("123456", "b", "fa", "fb", "na", "nb")
    assert t != pair.tag("123456", "a", "fa", "fb", "na", "nb")
    assert t != pair.tag("123457", "b", "fa", "fb", "na", "nb")
    assert t != pair.tag("123456", "b", "fx", "fb", "na", "nb")


def test_window_survives_typos_closes_after_max_tries_and_expires():
    p = pair.Pairing()
    w = p.open()
    assert len(w.pin) == 6 and w.pin.isdigit()
    assert pair.PAIR_WINDOW == 600.0
    for left in range(pair.MAX_TRIES - 1, 0, -1):
        assert p.fail(w) == left
        assert p.live() is w  # a typo does not close it
    assert p.fail(w) == 0 and p.live() is None  # the last wrong try does
    w2 = p.open()
    p.close(w2)
    assert p.live() is None  # a right PIN closes it
    p.open(now=time.time() - pair.PAIR_WINDOW - 1)
    assert p.live() is None  # expired


def test_identity_is_made_once(home):
    a = pair.load_identity()
    b = pair.load_identity()
    assert a is not None and b is not None
    assert a.node == b.node and a.fingerprint == b.fingerprint
    assert oct(a.key_path.stat().st_mode)[-3:] == "600"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_MACHINE_A = """
import json, sys, time
from cabildo import lan, store
store.connect().close()  # create the bus before threads race to
svc = lan.start_service(lambda m: print(m, file=sys.stderr, flush=True))
w = svc.pairing.open()
print(json.dumps({"pin": w.pin, "node": svc.me.node}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd["op"] == "post":
        c = store.connect()
        store.post_message(c, from_session="sa", from_handle=None, body=cmd["body"],
                           topic="proj:alpha", project="/a/path/alpha")
        c.close()
    elif cmd["op"] == "session":
        c = store.connect()
        store.register_session(c, "sa", cwd="/a/alpha", project="/a/alpha",
                               project_name="alpha", claude_pid=4242, owner_pid=4242)
        store.set_handle(c, "sa", "ana")
        c.close()
    elif cmd["op"] == "window":
        print(json.dumps({"pin": svc.pairing.open().pin}), flush=True)
    elif cmd["op"] == "join":
        c = store.connect()
        store.ensure_topic(c, cmd["topic"], created_by="sa")
        store.subscribe(c, "sa", cmd["topic"])
        c.commit()
        c.close()
    elif cmd["op"] == "say":
        c = store.connect()
        store.post_message(c, from_session="sa", from_handle="ana", body=cmd["body"],
                           topic=cmd["topic"], project="/a/alpha")
        c.close()
    elif cmd["op"] == "heard":
        c = store.connect()
        rows = [m["body"] for m in store.inbox(c, "sa", unread_only=False)]
        c.close()
        print(json.dumps(rows), flush=True)
    elif cmd["op"] == "bodies":
        c = store.connect()
        rows = [r["body"] for r in c.execute("SELECT body FROM messages ORDER BY id")]
        c.close()
        print(json.dumps(rows), flush=True)
"""


@pytest.fixture
def machine_a(tmp_path):
    home_a = tmp_path / "a"
    home_a.mkdir()
    port = _free_port()
    (home_a / "config.toml").write_text(f'[lan]\nport = {port}\nshare = ["alpha"]\n')
    env = {**os.environ, "CABILDO_HOME": str(home_a), "CABILDO_CONFIG": str(home_a / "config.toml")}
    proc = subprocess.Popen(
        [sys.executable, "-c", _MACHINE_A],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    hello = json.loads(proc.stdout.readline())
    yield proc, port, hello
    proc.kill()
    proc.wait()


def _ask(proc, cmd):
    proc.stdin.write(json.dumps(cmd) + "\n")
    proc.stdin.flush()
    if cmd["op"] in ("bodies", "heard", "window"):
        return json.loads(proc.stdout.readline())
    return None


def _wait(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def test_a_wrong_pin_costs_a_try_and_the_right_one_still_works(home, machine_a):
    _, port, hello = machine_a
    me = pair.load_identity()
    assert me is not None
    wrong = "000000" if hello["pin"] != "000000" else "111111"
    with pytest.raises(pair.PairError, match=r"wrong PIN \(4 tries left\)"):
        pair.join(me, "127.0.0.1", port, wrong, _free_port())
    assert pair.load_peers() == {}
    peer = pair.join(me, "127.0.0.1", port, hello["pin"], _free_port())
    assert peer.node == hello["node"]


def test_five_wrong_pins_close_the_window(home, machine_a):
    _, port, hello = machine_a
    me = pair.load_identity()
    assert me is not None
    wrong = "000000" if hello["pin"] != "000000" else "111111"
    for _ in range(pair.MAX_TRIES):
        with pytest.raises(pair.PairError, match="wrong PIN"):
            pair.join(me, "127.0.0.1", port, wrong, _free_port())
    with pytest.raises(pair.PairError, match="no pairing window"):
        pair.join(me, "127.0.0.1", port, hello["pin"], _free_port())
    assert pair.load_peers() == {}


def test_pair_then_messages_cross_both_ways(home, machine_a, monkeypatch):
    proc, port, hello = machine_a
    my_port = _free_port()
    monkeypatch.setattr(lan, "lan_settings", lambda: {"port": my_port, "share": ["alpha"]})
    svc = lan.start_service(lambda _m: None)
    assert svc is not None
    try:
        peer = pair.join(svc.me, "127.0.0.1", port, hello["pin"], my_port)
        assert peer.node == hello["node"]

        # A -> B: A writes, nudges B, B pulls.
        _ask(proc, {"op": "post", "body": "from A"})
        conn = store.connect()
        try:
            assert _wait(
                lambda: conn.execute("SELECT 1 FROM messages WHERE body='from A'").fetchone()
            )
            row = conn.execute("SELECT project, provenance FROM messages WHERE body='from A'")
            r = row.fetchone()
            assert (r["project"], r["provenance"]) == ("alpha", "lan")

            # B -> A
            store.post_message(
                conn, from_session="sb", from_handle=None, body="from B", topic="proj:alpha"
            )
        finally:
            conn.close()
        assert _wait(lambda: "from B" in (_ask(proc, {"op": "bodies"}) or []))
    finally:
        svc.close()


def test_unpaired_client_cannot_pull(home, machine_a):
    _, port, _ = machine_a
    me = pair.load_identity()
    assert me is not None
    fake = pair.PairedPeer(node="x", host="127.0.0.1", port=port, cert_pem=me.cert_pem, paired_at=0)
    # The server's cert is not ``fake.cert_pem``, so the pinned connect fails.
    with pytest.raises((OSError, ValueError)):
        pair.connect(me, "127.0.0.1", port, fake)


def test_cursor_file_lives_under_lan_dir(home):
    assert pair.lan_dir() == Path(store.home()) / "lan"


def test_daemon_dms_are_never_served_to_a_peer(conn):
    store.post_message(
        conn, from_session="daemon", from_handle="daemon", body="PIN 123456", to_session="s1"
    )
    store.post_message(conn, from_session="s1", from_handle=None, body="hi", topic="proj:a")
    rows, _ = lan.select_all(conn, 0, set(), "nodeB", frozenset({"a"}))
    assert [r["body"] for r in rows] == ["hi"]


def test_malformed_pair_request_does_not_burn_the_window(home, machine_a):
    _, port, hello = machine_a
    me = pair.load_identity()
    assert me is not None
    with pair.connect(me, "127.0.0.1", port, None) as s:
        pair.send_line(s, {"op": "pair", "proto": pair.PROTO})
        assert "malformed" in str((pair.recv_line(s) or {}).get("error"))
    pair.join(me, "127.0.0.1", port, hello["pin"], _free_port())
    assert hello["node"] in pair.load_peers()


def test_server_refuses_a_pull_from_an_unpaired_client(home, machine_a):
    _, port, hello = machine_a
    me = pair.load_identity()
    assert me is not None
    pair.join(me, "127.0.0.1", port, hello["pin"], _free_port())  # A now has one paired cert
    other = Path(str(home)) / "other"
    other.mkdir()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=x",
            "-keyout",
            str(other / "k"),
            "-out",
            str(other / "c"),
        ],
        capture_output=True,
        check=True,
    )
    stranger = pair.Identity(
        node="z", cert_path=other / "c", key_path=other / "k", cert_pem=(other / "c").read_text()
    )
    peer_a = pair.load_peers()[hello["node"]]
    with (
        pytest.raises((OSError, ValueError)),
        pair.connect(stranger, "127.0.0.1", port, peer_a) as s,
    ):
        pair.send_line(s, {"op": "pull", "proto": pair.PROTO, "since": 0})
        resp = pair.recv_line(s)
        if resp is None or not resp.get("ok"):
            raise OSError("refused")


def test_paired_peer_agents_show_up_as_remote_sessions(home, machine_a, monkeypatch):
    proc, port, hello = machine_a
    _ask(proc, {"op": "session"})
    my_port = _free_port()
    monkeypatch.setattr(lan, "lan_settings", lambda: {"port": my_port, "share": ["alpha"]})
    svc = lan.start_service(lambda _m: None)
    assert svc is not None
    try:
        pair.join(svc.me, "127.0.0.1", port, hello["pin"], my_port)
        svc.sync_once()
        conn = store.connect()
        try:
            row = store.get_session(conn, "sa")
        finally:
            conn.close()
        assert row is not None
        assert (row["handle"], row["client"]) == ("ana", f"lan:{hello['node']}")
    finally:
        svc.close()


def test_a_new_address_is_saved_only_where_the_pinned_cert_answers(home, machine_a):
    from cabildo import mdns

    _, port, hello = machine_a
    me = pair.load_identity()
    assert me is not None
    pair.join(me, "127.0.0.1", port, hello["pin"], _free_port())
    real = pair.load_peers()[hello["node"]]
    pair.save_peer(dataclasses.replace(real, host="127.0.0.2", port=1))
    svc = lan.Service(me, 0, lambda _m: None)

    def seen(p: int) -> mdns.Seen:
        return mdns.Seen(node=real.node, hosts=("127.0.0.1",), port=p, pairing=False, at=0)

    svc._on_seen(seen(_free_port()))  # forged: nothing with that cert listens there
    assert pair.load_peers()[real.node].port == 1
    svc._on_seen(seen(port))  # the real machine
    assert (pair.load_peers()[real.node].host, pair.load_peers()[real.node].port) == (
        "127.0.0.1",
        port,
    )


class _FakeMdns:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_pairing(self, flag: bool) -> None:
        self.calls.append(flag)

    def close(self) -> None:
        pass


def test_a_newer_window_keeps_its_pairing_flag(home):
    me = pair.load_identity()
    assert me is not None
    svc = lan.Service(me, 0, lambda _m: None)
    svc.mdns = _FakeMdns()
    svc.open_pairing()
    svc.pairing.close()  # the first window is used...
    svc.open_pairing()  # ...and a second opens before the first closer looks
    time.sleep(1.5)
    assert svc.mdns.calls == [True, True]
    svc.pairing.close()
    time.sleep(1.5)
    assert svc.mdns.calls == [True, True, False]


def test_no_flag_reset_after_the_daemon_stops(home):
    me = pair.load_identity()
    assert me is not None
    svc = lan.Service(me, 0, lambda _m: None)
    svc.mdns = _FakeMdns()
    svc.open_pairing()
    svc.stop.set()
    svc.pairing.close()
    time.sleep(1.5)
    assert svc.mdns.calls == [True]


def test_sync_records_what_each_pull_did(home, machine_a, monkeypatch):
    _, port, hello = machine_a
    my_port = _free_port()
    monkeypatch.setattr(lan, "lan_settings", lambda: {"port": my_port, "share": ["alpha"]})
    svc = lan.start_service(lambda _m: None)
    assert svc is not None
    try:
        pair.join(svc.me, "127.0.0.1", port, hello["pin"], my_port)
        svc.sync_once()
        st = lan.load_status()[hello["node"]]
        assert st.error == "" and st.at > 0
        # An unreachable peer is recorded with its reason, and the cursor is kept.
        peer = pair.load_peers()[hello["node"]]
        pair.save_peer(dataclasses.replace(peer, port=_free_port()))
        svc.sync_once()
        assert lan.load_status()[hello["node"]].error.startswith("unreachable")
    finally:
        svc.close()


def test_an_open_topic_joins_three_agents_in_three_projects_on_two_machines(
    home, machine_a, monkeypatch
):
    proc, port, hello = machine_a
    my_port = _free_port()
    monkeypatch.setattr(lan, "lan_settings", lambda: {"port": my_port, "share": ["alpha"]})
    svc = lan.start_service(lambda _m: None)
    assert svc is not None
    conn = store.connect()
    try:
        # Machine A: ana (project alpha). Machine B: bea (beta) and cy (gamma).
        _ask(proc, {"op": "session"})
        _ask(proc, {"op": "join", "topic": "war-room"})
        for sid, name in (("sb", "beta"), ("sc", "gamma")):
            store.register_session(
                conn,
                sid,
                cwd=f"/b/{name}",
                project=f"/b/{name}",
                project_name=name,
                claude_pid=100 + len(sid),
                owner_pid=100 + len(sid),
            )
            store.ensure_topic(conn, "war-room", created_by=sid)
            store.subscribe(conn, sid, "war-room")
        conn.commit()
        pair.join(svc.me, "127.0.0.1", port, hello["pin"], my_port)

        _ask(proc, {"op": "say", "topic": "war-room", "body": "from ana"})
        store.post_message(
            conn,
            from_session="sb",
            from_handle="bea",
            body="from bea",
            topic="war-room",
            project="/b/beta",
        )

        def heard(sid):
            return {m["body"] for m in store.inbox(conn, sid, unread_only=False)}

        assert _wait(lambda: {"from ana", "from bea"} <= heard("sc"))  # cy hears both
        assert _wait(lambda: "from ana" in heard("sb"))  # bea hears ana
        assert _wait(lambda: "from bea" in (_ask(proc, {"op": "heard"}) or []))  # ana hears bea
    finally:
        conn.close()
        svc.close()


def test_a_machine_that_is_already_paired_can_pair_again(home, machine_a):
    proc, port, hello = machine_a
    me = pair.load_identity()
    assert me is not None
    pair.join(me, "127.0.0.1", port, hello["pin"], _free_port())
    # A opens a second window; the same machine joins again (the server now has a
    # paired peer, so its TLS context asks for client certificates).
    window = _ask(proc, {"op": "window"})
    assert isinstance(window, dict)
    pin2 = window["pin"]
    again = pair.join(me, "127.0.0.1", port, pin2, _free_port())
    assert again.node == hello["node"]
