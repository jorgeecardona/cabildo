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


# --------------------------------------------------------------------------- #
# The chatroom: #general, joined by every session, paired or not
# --------------------------------------------------------------------------- #
def _topics(conn, sid):
    return store.subscriptions_of(conn, sid)


def test_every_session_joins_the_room_without_any_pairing(conn):
    _local(conn, "s1", "ana")
    assert {"general", "proj:alpha"} <= set(_topics(conn, "s1"))


def test_the_room_can_be_renamed_or_turned_off(conn, home):
    (home / "config.toml").write_text('[chat]\nroom = "den"\n')
    _local(conn, "s1", "ana")
    assert "den" in _topics(conn, "s1") and "general" not in _topics(conn, "s1")
    (home / "config.toml").write_text('[chat]\nroom = ""\n')
    _local(conn, "s2", "bob", pid=4343)
    assert "den" not in _topics(conn, "s2") and "general" not in _topics(conn, "s2")


def test_an_isolated_project_joins_the_room_but_not_a_project_channel(conn, home):
    (home / "config.toml").write_text("[project.alpha]\nisolated = true\n")
    _local(conn, "s1", "ana")
    assert "general" in _topics(conn, "s1") and "proj:alpha" not in _topics(conn, "s1")


def test_join_room_all_brings_in_sessions_that_were_already_open(conn):
    _local(conn, "s1", "ana")
    store.replace_remote_sessions(conn, "nodeB", [{"session_id": "r1", "project_name": "alpha"}])
    store.unsubscribe(conn, "s1", "general")
    assert store.join_room_all(conn) == 1  # the local session, not the remote row
    assert "general" in _topics(conn, "s1") and "general" not in _topics(conn, "r1")


def test_a_room_post_leaves_the_machine_even_from_an_excluded_project(conn):
    from cabildo import lan

    store.post_message(
        conn,
        from_session="s1",
        from_handle=None,
        body="hi room",
        topic="general",
        project="/repo/secret",
    )
    store.post_message(
        conn,
        from_session="s1",
        from_handle=None,
        body="private",
        topic="proj:secret",
        project="/repo/secret",
    )
    rows, _ = lan.select_all(conn, 0, {"secret"}, "nodeB", frozenset({"secret"}))
    assert [r["body"] for r in rows] == ["hi room"]


# --------------------------------------------------------------------------- #
# What a peer is allowed to receive
# --------------------------------------------------------------------------- #
def _serve(conn, peer="nodeB", exclude=(), share=()):
    from cabildo import lan

    rows, _ = lan.select_all(conn, 0, set(exclude), peer, frozenset(share))
    return {r["body"] for r in rows}


def _say(conn, body, **kw):
    kw.setdefault("from_session", "s1")
    return store.post_message(conn, from_handle=None, body=body, **kw)


def test_a_dm_between_two_local_agents_never_leaves(conn):
    _local(conn, "s1", "ana")
    _local(conn, "s2", "bob", pid=4343)
    store.replace_remote_sessions(conn, "nodeB", [{"session_id": "rb", "project_name": "x"}])
    store.replace_remote_sessions(conn, "nodeC", [{"session_id": "rc", "project_name": "x"}])
    _say(conn, "local dm", to_session="s2")
    _say(conn, "to b", to_session="rb")
    _say(conn, "to c", to_session="rc")
    assert _serve(conn, "nodeB") == {"to b"}  # only the peer's own agent's DM
    assert _serve(conn, "nodeC") == {"to c"}


def test_a_project_channel_goes_only_where_the_peer_has_that_project(conn):
    _local(conn, "s1", "ana")
    store.replace_remote_sessions(conn, "nodeB", [{"session_id": "rb", "project_name": "alpha"}])
    _say(conn, "alpha chatter", topic="proj:alpha", project="/repo/alpha")
    _say(conn, "other chatter", topic="proj:other", project="/repo/other")
    assert _serve(conn) == {"alpha chatter"}
    assert _serve(conn, share={"other"}) == {"alpha chatter", "other chatter"}
    assert _serve(conn, exclude={"alpha"}) == set()  # exclude beats both


def test_rooms_go_to_every_peer_but_relayed_rows_do_not(conn):
    _local(conn, "s1", "ana")
    _say(conn, "room post", topic="general")
    store.post_message(
        conn, from_session="rx", from_handle=None, body="relayed", topic="general", provenance="lan"
    )
    assert _serve(conn, "anyone") == {"room post"}
