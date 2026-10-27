"""Agents on a paired machine: stored as sessions rows with client='lan:<node>'."""

from __future__ import annotations

import time

from cabildo import mcp_server, store


def _local(conn, sid, handle, pid=4242):
    store.register_session(
        conn,
        sid,
        cwd="/repo/alpha",
        project="/repo/alpha",
        project_name="alpha",
        claude_pid=pid,
        owner_pid=pid,
    )
    store.set_handle(conn, sid, handle)


def test_export_skips_remote_rows_pidless_sessions_and_excluded_projects(conn):
    _local(conn, "s-local", "ana")
    store.register_session(conn, "s-nopid", cwd="/repo/alpha", project_name="alpha")
    store.replace_remote_sessions(
        conn, "nodeB", [{"session_id": "s-remote", "project_name": "alpha"}]
    )
    assert [r["session_id"] for r in store.export_sessions(conn, set())] == ["s-local"]
    assert store.export_sessions(conn, {"alpha"}) == []


def test_replace_sets_the_list_and_borrows_the_local_project_path(conn):
    _local(conn, "s-local", "ana")
    store.replace_remote_sessions(
        conn, "nodeB", [{"session_id": "r1", "handle": "bob", "project_name": "alpha"}]
    )
    row = store.get_session(conn, "r1")
    assert row is not None
    assert (row["client"], row["project"], row["owner_pid"]) == ("lan:nodeB", "/repo/alpha", None)
    store.replace_remote_sessions(conn, "nodeB", [])
    assert store.get_session(conn, "r1") is None


def test_replace_never_overwrites_a_local_row(conn):
    _local(conn, "same-id", "ana")
    store.replace_remote_sessions(conn, "nodeB", [{"session_id": "same-id", "handle": "evil"}])
    row = store.get_session(conn, "same-id")
    assert row is not None and (row["handle"], row["client"]) == ("ana", "claude")


def test_prune_drops_remote_rows_not_seen_for_the_active_window(conn):
    old = time.time() - store.ACTIVE_WINDOW - 60
    store.replace_remote_sessions(
        conn, "nodeB", [{"session_id": "r-old", "last_seen_at": old}, {"session_id": "r-new"}]
    )
    assert store.prune_remote_sessions(conn) == 1
    assert store.get_session(conn, "r-new") is not None


def test_send_to_handle_prefers_the_local_agent(conn):
    _local(conn, "s-local", "bob")
    store.replace_remote_sessions(conn, "nodeB", [{"session_id": "r1", "handle": "bob"}])
    assert mcp_server._resolve_recipient(conn, "@bob") == "s-local"
    store.replace_remote_sessions(conn, "nodeB", [{"session_id": "r1", "handle": "carol"}])
    assert mcp_server._resolve_recipient(conn, "@carol") == "r1"


def test_node_label_only_for_remote_rows(conn):
    _local(conn, "s-local", "ana")
    store.replace_remote_sessions(conn, "abcdef1234", [{"session_id": "r1"}])
    assert mcp_server._node(store.get_session(conn, "r1")) == "abcdef12"
    assert mcp_server._node(store.get_session(conn, "s-local")) is None
