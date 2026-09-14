"""Peer-to-peer LAN sync (cabildo.lan): the guid foundation, project-scoped
selection, guid dedup / merge, and a real localhost TCP pull round-trip.

Guarded by importorskip: lan.py is a new module, and the land daemon's gate
imports the stale main checkout (no lan module), so this whole file skips there.
CI installs the branch and is the authoritative check.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

lan = pytest.importorskip("cabildo.lan")

from cabildo import store


def _connect(path):
    """A store.connect() against an explicit file, so one test can hold two
    independent buses ('two machines') without racing CABILDO_HOME.
    """
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(store.SCHEMA)
    store._migrate(c)
    return c


def _post(conn, body, *, project_name="alpha", **kw):
    return store.post_message(
        conn,
        from_session="s1",
        from_handle=None,
        body=body,
        topic=f"proj:{project_name}",
        **kw,
    )


# --------------------------------------------------------------------------- #
# The guid foundation
# --------------------------------------------------------------------------- #
def test_post_message_mints_a_guid(conn):
    mid = _post(conn, "hi")
    row = conn.execute("SELECT guid FROM messages WHERE id=?", (mid,)).fetchone()
    assert row["guid"] and len(row["guid"]) >= 16


def test_post_message_is_idempotent_by_guid(conn):
    first = _post(conn, "once", guid="fixed-guid")
    again = _post(conn, "once", guid="fixed-guid")
    assert first == again  # same row id, not a duplicate
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 1


def test_migrate_backfills_guid_on_an_existing_bus(conn):
    # Model a pre-guid bus: a messages table without the column or its index,
    # holding a row, then run the migration and assert it got a guid.
    conn.execute("ALTER TABLE messages RENAME TO messages_old")
    conn.execute("DROP INDEX IF EXISTS idx_messages_guid")
    conn.execute(
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,"
        " from_session TEXT NOT NULL, from_handle TEXT, topic TEXT,"
        " to_session TEXT, project TEXT, body TEXT NOT NULL,"
        " urgent INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO messages(created_at, from_session, body, topic) VALUES (?,?,?,?)",
        (1.0, "s1", "old", "proj:alpha"),
    )
    conn.commit()
    store._migrate(conn)
    missing = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE guid IS NULL").fetchone()["n"]
    assert missing == 0


# --------------------------------------------------------------------------- #
# Project-scoped selection + cursor advance
# --------------------------------------------------------------------------- #
def test_select_outbound_filters_by_project_and_reports_scanned_max(conn):
    a1 = _post(conn, "a1", project_name="alpha")
    a2 = _post(conn, "a2", project_name="alpha")
    b1 = _post(conn, "b1", project_name="beta")

    rows, max_id = lan.select_outbound(conn, "alpha", 0)
    assert [r["id"] for r in rows] == [a1, a2]
    # max_id advances past the filtered-out beta row so it is never re-scanned.
    assert max_id == b1

    rows2, _ = lan.select_outbound(conn, "alpha", since=a2)
    assert rows2 == []


# --------------------------------------------------------------------------- #
# Merge / dedup / cross-hop identity
# --------------------------------------------------------------------------- #
def test_merge_incoming_dedups_by_guid_and_preserves_identity(conn):
    rows = [
        {
            "guid": "g1",
            "created_at": 111.0,
            "from_session": "peer",
            "from_handle": "bob",
            "topic": "proj:alpha",
            "to_session": None,
            "project": None,
            "body": "one",
            "urgent": 0,
        },
        {
            "guid": "g2",
            "created_at": 222.0,
            "from_session": "peer",
            "from_handle": "bob",
            "topic": "proj:alpha",
            "to_session": None,
            "project": None,
            "body": "two",
            "urgent": 1,
        },
    ]
    assert lan.merge_incoming(conn, rows) == 2
    assert lan.merge_incoming(conn, rows) == 0  # idempotent second pass

    got = conn.execute("SELECT guid, created_at, origin FROM messages ORDER BY guid").fetchall()
    assert [r["guid"] for r in got] == ["g1", "g2"]
    assert all(r["origin"] == "lan" for r in got)
    assert got[0]["created_at"] == 111.0  # original timestamp kept across the hop


def test_merge_skips_malformed_rows(conn):
    both = [
        {
            "guid": "gx",
            "created_at": 1.0,
            "from_session": "p",
            "topic": "proj:alpha",
            "to_session": "s9",
            "body": "bad",
            "urgent": 0,
        }
    ]  # both topic and to_session
    assert lan.merge_incoming(conn, both) == 0


def test_no_echo_loop_across_two_buses(tmp_path):
    src, dst = _connect(tmp_path / "src.db"), _connect(tmp_path / "dst.db")
    try:
        _post(src, "hello", project_name="alpha")
        rows, _ = lan.select_outbound(src, "alpha", 0)
        assert lan.merge_incoming(dst, rows) == 1

        # dst serves the lan-origin row onward; src already has that guid, so
        # pulling it back merges nothing — no infinite bounce.
        back, _ = lan.select_outbound(dst, "alpha", 0)
        assert back and back[0]["guid"] == rows[0]["guid"]
        assert lan.merge_incoming(src, back) == 0
    finally:
        src.close()
        dst.close()


# --------------------------------------------------------------------------- #
# Real localhost TCP pull round-trip
# --------------------------------------------------------------------------- #
def test_tcp_pull_round_trip(home, tmp_path):
    path_a = tmp_path / "peer_a.db"
    conn_a = _connect(path_a)
    _post(conn_a, "from A", project_name="alpha")

    srv = lan.open_server_socket(0)
    port = srv.getsockname()[1]
    stop = threading.Event()
    t = threading.Thread(
        target=lan.run_server,
        args=(srv, "alpha", lambda: _connect(path_a), None, stop),
        daemon=True,
    )
    t.start()
    try:
        rows, max_id = lan.pull_from_peer("127.0.0.1", port, "alpha", 0, None, "node-b")
        assert len(rows) == 1
        assert rows[0]["body"] == "from A"
        assert max_id == rows[0]["id"]

        conn_b = _connect(tmp_path / "peer_b.db")
        try:
            assert lan.merge_incoming(conn_b, rows) == 1
            got = conn_b.execute("SELECT body, guid, origin FROM messages").fetchone()
            assert got["body"] == "from A"
            assert got["guid"] == rows[0]["guid"]
            assert got["origin"] == "lan"
        finally:
            conn_b.close()
    finally:
        stop.set()
        srv.close()
        t.join(timeout=3)
        conn_a.close()


def test_tcp_pull_rejects_wrong_secret(home, tmp_path):
    path_a = tmp_path / "peer_a.db"
    conn_a = _connect(path_a)
    _post(conn_a, "secret msg", project_name="alpha")

    srv = lan.open_server_socket(0)
    port = srv.getsockname()[1]
    stop = threading.Event()
    t = threading.Thread(
        target=lan.run_server,
        args=(srv, "alpha", lambda: _connect(path_a), "hunter2", stop),
        daemon=True,
    )
    t.start()
    try:
        rows, since = lan.pull_from_peer("127.0.0.1", port, "alpha", 0, "wrong", "node-b")
        assert rows == []
        assert since == 0  # cursor holds on a refused pull

        ok_rows, _ = lan.pull_from_peer("127.0.0.1", port, "alpha", 0, "hunter2", "node-b")
        assert len(ok_rows) == 1
    finally:
        stop.set()
        srv.close()
        t.join(timeout=3)
        conn_a.close()
