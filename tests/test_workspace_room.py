"""Workspace rooms: sessions under a workspace folder share a room, so they may DM
and wake each other across projects; plain room posts do not wake.
"""

from __future__ import annotations

import json

from cabildo import cli, store
from tests.conftest import make_session


def _wakes(capsys, sid):
    cli.main(["pull", "--session", sid, "--json"])
    return {m["preview"]: m["wakes"] for m in json.loads(capsys.readouterr().out)["messages"]}


def _set_cwd(conn, sid, cwd):
    conn.execute("UPDATE sessions SET cwd=? WHERE session_id=?", (cwd, sid))
    conn.commit()


def test_workspace_topic_is_unique_per_full_path():
    a = store.workspace_topic("/x/api")
    assert a.startswith("ws:api-")
    assert a != store.workspace_topic("/y/api")


def test_running_and_new_sessions_join_the_workspace_room(conn, tmp_path):
    ws = tmp_path / "ws"
    (ws / "alpha").mkdir(parents=True)
    (ws / "beta").mkdir()
    make_session(conn, "A", handle="alice", project="/r/alpha", project_name="alpha")
    _set_cwd(conn, "A", str(ws / "alpha"))
    topic = store.register_workspace(conn, str(ws))
    assert topic in store.subscriptions_of(conn, "A")  # was already running
    store.register_session(conn, "B", cwd=str(ws / "beta"), project="/r/beta", project_name="beta")
    assert topic in store.subscriptions_of(conn, "B")  # started later
    store.register_session(conn, "C", cwd=str(tmp_path), project="/r/c", project_name="c")
    assert topic not in store.subscriptions_of(conn, "C")  # outside the folder


def test_members_of_different_projects_dm_and_wake_but_posts_do_not(conn, tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "alpha").mkdir(parents=True)
    (ws / "beta").mkdir()
    for sid, name in (("A", "alpha"), ("B", "beta")):
        make_session(conn, sid, handle=sid.lower(), project=f"/r/{name}", project_name=name)
        store.unsubscribe(conn, sid, "general")
        _set_cwd(conn, sid, str(ws / name))
    conn.commit()
    topic = store.register_workspace(conn, str(ws))
    assert store.consent_error(conn, "A", to_session="B") is None
    store.post_message(conn, from_session="A", from_handle="a", body="dm", to_session="B")
    store.post_message(conn, from_session="A", from_handle="a", body="post", topic=topic)
    store.post_message(conn, from_session="A", from_handle="a", body="hi @b", topic=topic)
    rows = _wakes(capsys, "B")
    assert rows == {"dm": True, "post": False, "hi @b": True}


def test_forget_empties_the_room(conn, tmp_path):
    make_session(conn, "A", handle="a", project="/r/a", project_name="a")
    _set_cwd(conn, "A", str(tmp_path))
    topic = store.register_workspace(conn, str(tmp_path))
    assert store.forget_workspace(conn, str(tmp_path)) is True
    assert topic not in store.subscriptions_of(conn, "A")
    assert store.forget_workspace(conn, str(tmp_path)) is False
