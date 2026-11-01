"""Daemon start fails intents that a dead daemon left running."""

from __future__ import annotations


def test_daemon_start_fails_intents_of_a_dead_daemon(conn):
    import socket

    from cabildo import daemon

    conn.execute(
        "INSERT INTO intents(created_at, from_session, action, params, status, worker, started_at) "
        "VALUES(?, 'x', 'push', '{}', 'running', ?, ?)",
        (1.0, f"{socket.gethostname()}:999999999", 1.0),
    )
    conn.commit()
    assert daemon._reap_dead_workers(conn) == 1
    row = conn.execute("SELECT status FROM intents").fetchone()
    assert row["status"] == "failed"
