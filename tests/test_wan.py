"""WAN sync: the explicit-peer variant of lan.py, stamping provenance='wan'.

Phase 4 of the broker refactor (docs/design/broker-service.md). wan.py reuses
lan.py's pull/dedup, so these tests pin the new glue: the provenance stamp, the
per-peer cursor, and the token-gated socket round trip. The daemon runs it
through ``wan.Service``.
"""

from __future__ import annotations

import threading

from cabildo import lan, store, wan


def _msg(guid: str, body: str) -> dict:
    return {
        "guid": guid,
        "created_at": 1.0,
        "from_session": "remote",
        "from_handle": "r",
        "topic": "general",
        "to_session": None,
        "project": None,
        "body": body,
        "urgent": 0,
    }


def test_merge_incoming_stamps_wan_provenance(home):
    conn = store.connect()
    rows = [
        {
            "guid": "g1",
            "created_at": 1000.0,
            "from_session": "peerA",
            "from_handle": "a",
            "topic": "general",
            "to_session": None,
            "project": None,
            "body": "hi from A",
            "urgent": 0,
        }
    ]
    assert lan.merge_incoming(conn, rows, provenance=wan.ORIGIN) == 1
    row = conn.execute("SELECT provenance, body FROM messages WHERE guid='g1'").fetchone()
    assert row["provenance"] == "wan"
    assert row["body"] == "hi from A"
    # Re-merging the same guid is a no-op (dedup across the hop).
    assert lan.merge_incoming(conn, rows, provenance=wan.ORIGIN) == 0
    conn.close()


def test_sync_round_merges_and_advances_cursor(home, monkeypatch):
    canned = [
        {
            "guid": "g9",
            "created_at": 1.0,
            "from_session": "remote",
            "from_handle": "r",
            "topic": "general",
            "to_session": None,
            "project": None,
            "body": "remote hi",
            "urgent": 0,
        }
    ]

    def fake_pull(host, port, project, since, secret, node):
        return (canned, 42, "peer-A") if since == 0 else ([], since, "peer-A")

    monkeypatch.setattr(lan, "pull_from_peer", fake_pull)
    peers = [wan.Peer("100.64.0.2", 48653)]

    res = wan.sync_round(peers, "proj", "tok")
    assert res["merged"] == 1

    conn = store.connect()
    assert (
        conn.execute("SELECT provenance FROM messages WHERE guid='g9'").fetchone()["provenance"]
        == "wan"
    )
    conn.close()

    # Cursor advanced to 42, so a second round pulls nothing new.
    assert wan.sync_round(peers, "proj", "tok")["merged"] == 0


def test_sync_round_repulls_when_the_peer_node_changes(home, monkeypatch):
    # A peer whose DB reset comes back with a new node id and ids restarted at 1.
    # The stale cursor sits above them; the fix must notice the new node and
    # re-pull from 0 instead of stalling forever (finding #2).
    gen_a = [_msg("a1", "from gen A")]
    gen_b = [_msg("b1", "from gen B (after reset)")]
    state = {"node": "A"}

    def fake_pull(host, port, project, since, secret, node):
        if state["node"] == "A":
            return (gen_a, 9, "A") if since == 0 else ([], since, "A")
        # After the reset (node B), the message only comes back on a from-zero pull.
        return (gen_b, 3, "B") if since == 0 else ([], since, "B")

    monkeypatch.setattr(lan, "pull_from_peer", fake_pull)
    peers = [wan.Peer("box", 48653)]

    assert wan.sync_round(peers, "proj", None)["merged"] == 1  # gen A synced, cursor -> 9
    state["node"] = "B"  # the box was reinstalled at the same address
    assert wan.sync_round(peers, "proj", None)["merged"] == 1  # noticed reset, re-pulled

    conn = store.connect()
    provenances = {
        r["guid"]: r["provenance"] for r in conn.execute("SELECT guid, provenance FROM messages")
    }
    conn.close()
    assert provenances.get("a1") == "wan"
    assert provenances.get("b1") == "wan"


def test_serve_and_pull_over_socket_with_token(home):
    conn = store.connect()
    store.post_message(
        conn, from_session="me", from_handle="me", body="wire msg", topic="proj:demo"
    )
    conn.close()

    sock = wan.open_server("127.0.0.1", 0)
    assert sock.getsockname()[0] == "127.0.0.1"  # loopback, never 0.0.0.0
    port = sock.getsockname()[1]
    stop = threading.Event()
    t = threading.Thread(target=wan.serve, args=(sock, "demo", "s3cret", stop), daemon=True)
    t.start()
    try:
        rows, _, _ = lan.pull_from_peer("127.0.0.1", port, "demo", 0, "s3cret", "nodeB")
        assert "wire msg" in [r["body"] for r in rows]
        # A wrong token is refused, so the puller gets nothing.
        bad, _, _ = lan.pull_from_peer("127.0.0.1", port, "demo", 0, "wrong", "nodeB")
        assert bad == []
    finally:
        stop.set()
        sock.close()
        t.join(timeout=5)


def _config(home_dir, body: str) -> None:
    (store.home() / "config.toml").write_text(body, encoding="utf-8")


LINK = """
[project.proj.remote]
peers = ["1.2.3.4:48653"]
token = "secret-value"
interval = 60
"""


def test_load_links_reads_the_project_tables(home):
    _config(home, LINK)
    assert wan.load_links() == [
        wan.Link("proj", (wan.Peer("1.2.3.4", 48653),), "secret-value", None, 60.0)
    ]


def test_no_table_means_no_service(home):
    assert wan.start_service(lambda _m: None) is None


def test_service_pulls_once_per_interval_and_records_status(home, monkeypatch):
    _config(home, LINK)
    calls = []

    def pull(host, port, project, since, secret, node):
        calls.append(project)
        return [_msg("g5", "from the box")], 7, "boxnode"

    monkeypatch.setattr(lan, "pull_from_peer", pull)
    svc = wan.Service(wan.load_links(), lambda _m: None)
    assert svc.sync_due(1000.0) == 1
    assert svc.sync_due(1030.0) == 0  # inside the interval
    assert calls == ["proj"]
    [row] = wan.peers_report()
    assert row["address"] == "1.2.3.4:48653"
    assert row["last_pull"]["merged"] == 1
    assert "secret-value" not in str(row)


def test_status_records_an_unreachable_peer(home, monkeypatch):
    _config(home, LINK)
    monkeypatch.setattr(lan, "pull_from_peer", lambda *_: ([], 0, None))
    wan.Service(wan.load_links(), lambda _m: None).sync_due(1000.0)
    assert wan.peers_report()[0]["last_pull"]["error"] == "unreachable or refused"


def test_cursor_is_per_project_on_one_address(home, monkeypatch):
    seen = []

    def pull(host, port, project, since, secret, node):
        seen.append((project, since))
        return [], 9, "n"

    monkeypatch.setattr(lan, "pull_from_peer", pull)
    peer = [wan.Peer("h", 1)]
    wan.sync_round(peer, "a", "t")
    wan.sync_round(peer, "b", "t")
    assert seen == [("a", 0), ("b", 0)]


def test_service_refuses_an_unsafe_bind(home):
    logs = []
    for bind, token in (("0.0.0.0", "t"), ("127.0.0.1", None)):  # noqa: S104
        link = wan.Link("p", (), token, wan.Peer(bind, 0), 30.0)
        svc = wan.Service([link], logs.append)
        svc._serve(link, link.bind)
        assert svc.servers == []
    assert all("refusing" in m for m in logs)


def test_service_serves_on_loopback_and_a_peer_can_pull(home):
    link = wan.Link("proj", (), "tok", wan.Peer("127.0.0.1", 0), 30.0)
    svc = wan.Service([link], lambda _m: None)
    svc._serve(link, link.bind)
    try:
        host, port = svc.servers[0].getsockname()[:2]
        _rows, _top, node = lan.pull_from_peer(host, port, "proj", 0, "tok", "other")
        assert node is not None
        assert lan.pull_from_peer(host, port, "proj", 0, "wrong", "other")[2] is None
    finally:
        svc.close()


def test_mcp_peers_tool_lists_wan_peers(home):
    from cabildo import mcp_server

    _config(home, LINK)
    assert [p["address"] for p in mcp_server.peers()] == ["1.2.3.4:48653"]
