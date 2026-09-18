"""WAN sync: the explicit-peer variant of lan.py, stamping origin='wan'.

Phase 4 of the broker refactor (docs/design/broker-service.md). wan.py reuses
lan.py's pull/dedup, so these tests pin the new glue: the origin stamp, the
per-peer cursor, and the token-gated socket round trip. Additive -- nothing in
the live daemon runs this yet.
"""

from __future__ import annotations

import threading

from cabildo import lan, store, wan


def test_merge_incoming_stamps_wan_origin(home):
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
    assert lan.merge_incoming(conn, rows, origin=wan.ORIGIN) == 1
    row = conn.execute("SELECT origin, body FROM messages WHERE guid='g1'").fetchone()
    assert row["origin"] == "wan"
    assert row["body"] == "hi from A"
    # Re-merging the same guid is a no-op (dedup across the hop).
    assert lan.merge_incoming(conn, rows, origin=wan.ORIGIN) == 0
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
        return (canned, 42) if since == 0 else ([], since)

    monkeypatch.setattr(lan, "pull_from_peer", fake_pull)
    peers = [wan.Peer("100.64.0.2", 48653)]

    res = wan.sync_round(peers, "proj", "tok")
    assert res["merged"] == 1

    conn = store.connect()
    assert conn.execute("SELECT origin FROM messages WHERE guid='g9'").fetchone()["origin"] == "wan"
    conn.close()

    # Cursor advanced to 42, so a second round pulls nothing new.
    assert wan.sync_round(peers, "proj", "tok")["merged"] == 0


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
        rows, _ = lan.pull_from_peer("127.0.0.1", port, "demo", 0, "s3cret", "nodeB")
        assert "wire msg" in [r["body"] for r in rows]
        # A wrong token is refused, so the puller gets nothing.
        bad, _ = lan.pull_from_peer("127.0.0.1", port, "demo", 0, "wrong", "nodeB")
        assert bad == []
    finally:
        stop.set()
        sock.close()
        t.join(timeout=5)


def test_run_single_round_pull_only_merges(home, monkeypatch):
    canned = [
        {
            "guid": "g5",
            "created_at": 2.0,
            "from_session": "remote",
            "from_handle": "r",
            "topic": "general",
            "to_session": None,
            "project": None,
            "body": "from the box",
            "urgent": 0,
        }
    ]
    monkeypatch.setattr(lan, "pull_from_peer", lambda *_: (canned, 7))
    rc = wan.run(
        "proj",
        peers=["1.2.3.4:48653"],
        token="t",  # noqa: S106 - test token, not a real secret
        bind=None,
        interval=1,
        loop=False,
    )
    assert rc == 0
    conn = store.connect()
    assert conn.execute("SELECT origin FROM messages WHERE guid='g5'").fetchone()["origin"] == "wan"
    conn.close()


def test_run_serves_on_bind_and_returns(home):
    # bind on an ephemeral loopback port, no peers: one round serves and returns.
    rc = wan.run("proj", peers=[], token=None, bind="127.0.0.1:0", interval=1, loop=False)
    assert rc == 0
