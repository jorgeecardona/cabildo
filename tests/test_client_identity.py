"""Client-independent session identity and lifecycle (#6).

Cabildo must let Claude Code and Codex sessions share one local bus without
confusing identities or leaving crashed agents active. These tests cover the
acceptance criteria: an additive migration of an existing database, concurrent
distinct clients, stable identity across MCP reconnect/resume and threads
sharing one host process, crash cleanup with claim release, and exposure of the
client through the presence tools.
"""

from __future__ import annotations

import sqlite3

import pytest

from cabildo import mcp_server as srv
from cabildo import store


def _row(conn, sid: str) -> sqlite3.Row:
    """get_session that must hit — narrows the Optional for the subscripts below."""
    row = store.get_session(conn, sid)
    assert row is not None, f"session {sid!r} not found"
    return row


# --------------------------------------------------------------------------- #
# Store layer: schema, migration, generic lifecycle
# --------------------------------------------------------------------------- #
def test_register_defaults_client_claude_and_mirrors_owner_pid(conn):
    store.register_session(conn, "s", claude_pid=1000, project_name="alpha")
    row = _row(conn, "s")
    assert row["client"] == "claude"
    # owner_pid is not passed: it mirrors claude_pid so Claude callers (the
    # hooks) get the generic lifecycle without changing their call.
    assert row["owner_pid"] == 1000


def test_register_explicit_client_keeps_claude_pid_null(conn):
    store.register_session(conn, "cx", client="codex", owner_pid=7777, project_name="alpha")
    row = _row(conn, "cx")
    assert row["client"] == "codex"
    assert row["owner_pid"] == 7777
    assert row["claude_pid"] is None


def test_reregister_without_client_preserves_codex(conn):
    store.register_session(conn, "cx", client="codex", owner_pid=1, project_name="alpha")
    # A defensive re-register (default client) must not downgrade the row.
    store.register_session(conn, "cx", project_name="alpha")
    assert _row(conn, "cx")["client"] == "codex"


def test_session_for_owner_is_scoped_by_client(conn):
    # Two clients that happen to share one host pid stay separable by client.
    store.register_session(conn, "cx", client="codex", owner_pid=500, project_name="alpha")
    store.register_session(conn, "cl", client="claude", claude_pid=500, project_name="alpha")
    assert store.session_for_owner(conn, 500, "codex") == "cx"
    assert store.session_for_owner(conn, 500, "claude") == "cl"
    assert store.session_for_owner(conn, 500, "gemini") is None
    assert store.session_for_owner(conn, 0, "codex") is None


def test_session_for_owner_ignores_ended(conn):
    store.register_session(conn, "cx", client="codex", owner_pid=600, project_name="alpha")
    store.end_session(conn, "cx")
    assert store.session_for_owner(conn, 600, "codex") is None


def test_reap_keys_on_owner_pid_and_releases_claims(conn):
    store.register_session(
        conn, "cx", client="codex", owner_pid=9999, project="/repo/alpha", project_name="alpha"
    )
    store.acquire_claim(
        conn,
        resource="build",
        holder_session="cx",
        holder_handle=None,
        project="/repo/alpha",
        ttl_seconds=300,
        note=None,
    )
    assert any(c["resource"] == "build" for c in store.list_claims(conn))

    reaped = store.reap_dead_sessions(conn, is_alive=lambda pid: pid != 9999)

    assert reaped == 1
    assert _row(conn, "cx")["status"] == "ended"
    # A crashed session must not keep holding resources.
    assert not any(c["resource"] == "build" for c in store.list_claims(conn))


def test_reap_falls_back_to_claude_pid_when_owner_null(conn):
    # A pre-migration row could have owner_pid still NULL; liveness must fall
    # back to claude_pid so those rows are still reaped.
    store.register_session(conn, "s", claude_pid=777, project_name="alpha")
    conn.execute("UPDATE sessions SET owner_pid=NULL WHERE session_id='s'")
    conn.commit()

    reaped = store.reap_dead_sessions(conn, is_alive=lambda pid: False)

    assert reaped == 1
    assert _row(conn, "s")["status"] == "ended"


def test_reap_leaves_live_and_pidless_sessions(conn):
    store.register_session(conn, "live", client="codex", owner_pid=42, project_name="alpha")
    store.register_session(conn, "human", project_name="alpha")  # no owning pid
    reaped = store.reap_dead_sessions(conn, is_alive=lambda pid: True)
    assert reaped == 0
    assert _row(conn, "live")["status"] == "active"
    assert _row(conn, "human")["status"] == "active"


def test_existing_database_migrates_and_backfills(home):
    """A database created before #6 (no client/owner_pid) gains the columns and
    backfills every row to client='claude', owner_pid=claude_pid, on connect.
    """
    db = store.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    raw.executescript(
        """
        CREATE TABLE sessions (
            session_id    TEXT PRIMARY KEY,
            handle        TEXT,
            cwd           TEXT,
            project       TEXT,
            project_name  TEXT,
            claude_pid    INTEGER,
            tag           TEXT,
            status        TEXT NOT NULL DEFAULT 'active',
            created_at    REAL NOT NULL,
            last_seen_at  REAL NOT NULL
        );
        """
    )
    raw.execute(
        "INSERT INTO sessions(session_id, claude_pid, status, created_at, last_seen_at) "
        "VALUES ('old-claude', 4242, 'active', 1.0, 1.0)"
    )
    raw.commit()
    raw.close()

    conn = store.connect()  # runs SCHEMA (no-op on the old table) + _migrate
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert {"client", "owner_pid"} <= cols
        row = _row(conn, "old-claude")
        assert row["client"] == "claude"
        assert row["owner_pid"] == 4242
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Identity resolution in the MCP server (_me)
# --------------------------------------------------------------------------- #
@pytest.fixture
def me_env(conn, monkeypatch):
    """Drive _me deterministically: no inherited env, a settable claude pid
    (the rendezvous) and owner ppid (the generic anchor).
    """
    for var in ("CABILDO_TAG", "CABILDO_CLIENT", "CABILDO_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    state = {"claude_pid": 0, "ppid": 4000}
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: state["claude_pid"])
    monkeypatch.setattr(srv.os, "getppid", lambda: state["ppid"])
    return state


def test_me_claude_pid_rendezvous_preserved(me_env, conn):
    # Regression: an ordinary Claude terminal still binds via the PID walk.
    store.register_session(
        conn, "claude-sess", claude_pid=111, project="/repo/alpha", project_name="alpha"
    )
    me_env["claude_pid"] = 111
    me = srv.whoami()
    assert me["session_id"] == "claude-sess"
    assert me["client"] == "claude"


def test_me_explicit_codex_session(me_env, monkeypatch, conn):
    monkeypatch.setenv("CABILDO_CLIENT", "codex")
    monkeypatch.setenv("CABILDO_SESSION_ID", "codex-abc")
    me = srv.whoami()
    assert me["session_id"] == "codex-abc"
    assert me["client"] == "codex"
    row = _row(conn, "codex-abc")
    assert row["owner_pid"] == 4000  # from ppid, the generic owner anchor
    assert row["claude_pid"] is None


def test_me_explicit_session_survives_reconnect(me_env, monkeypatch):
    # MCP reconnect / resume: a new server process (new parent) but the client
    # states the same session id, so identity is stable.
    monkeypatch.setenv("CABILDO_CLIENT", "codex")
    monkeypatch.setenv("CABILDO_SESSION_ID", "codex-abc")
    assert srv.whoami()["session_id"] == "codex-abc"
    me_env["ppid"] = 9191
    assert srv.whoami()["session_id"] == "codex-abc"


def test_me_threads_sharing_process_stay_distinct(me_env, monkeypatch, conn):
    # Several sessions in one host process: the shared pid is ambiguous, so the
    # explicit id is what keeps them apart.
    monkeypatch.setenv("CABILDO_CLIENT", "codex")
    monkeypatch.setenv("CABILDO_SESSION_ID", "codex-t1")
    assert srv.whoami()["session_id"] == "codex-t1"
    monkeypatch.setenv("CABILDO_SESSION_ID", "codex-t2")
    assert srv.whoami()["session_id"] == "codex-t2"
    assert store.get_session(conn, "codex-t1") is not None
    assert store.get_session(conn, "codex-t2") is not None


def test_me_provisional_non_claude_is_client_scoped(me_env, monkeypatch):
    # No explicit id, not Claude: a provisional id anchored on the owner pid and
    # prefixed by the client so it can never collide with Claude's `pid-<n>`.
    monkeypatch.setenv("CABILDO_CLIENT", "codex")
    me_env["ppid"] = 5555
    me = srv.whoami()
    assert me["session_id"] == "codex-pid-5555"
    assert me["client"] == "codex"


def test_me_provisional_claude_keeps_pid_prefix(me_env):
    # No ancestry, no explicit id: Claude keeps its historic `pid-<n>` format.
    me_env["claude_pid"] = 0
    me = srv.whoami()
    assert me["session_id"].startswith("pid-")
    assert me["client"] == "claude"


def test_concurrent_claude_and_codex_are_distinct(me_env, monkeypatch, conn):
    # Same checkout, two live clients: two rows, two clients, one bus.
    store.register_session(
        conn, "claude-sess", claude_pid=111, project="/repo/alpha", project_name="alpha"
    )
    monkeypatch.setenv("CABILDO_CLIENT", "codex")
    monkeypatch.setenv("CABILDO_SESSION_ID", "codex-abc")
    assert srv.whoami()["session_id"] == "codex-abc"

    monkeypatch.delenv("CABILDO_CLIENT")
    monkeypatch.delenv("CABILDO_SESSION_ID")
    me_env["claude_pid"] = 111
    assert srv.whoami()["session_id"] == "claude-sess"

    active = {r["session_id"]: r["client"] for r in store.active_sessions(conn)}
    assert active.get("claude-sess") == "claude"
    assert active.get("codex-abc") == "codex"


def test_list_agents_exposes_client(me_env, conn):
    store.register_session(
        conn, "me-claude", claude_pid=111, project="/repo/alpha", project_name="alpha"
    )
    store.register_session(
        conn,
        "peer-codex",
        client="codex",
        owner_pid=222,
        project="/repo/alpha",
        project_name="alpha",
    )
    me_env["claude_pid"] = 111
    peers = {a["session_id"]: a for a in srv.list_agents()}
    assert peers["peer-codex"]["client"] == "codex"
