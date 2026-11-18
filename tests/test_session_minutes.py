"""Session minutes (#31): left by a session or the human, append-only, read back by project."""

from __future__ import annotations

import pytest

from cabildo import mcp_server as srv
from cabildo import store


def _add(conn, title="t", **kw):
    kw.setdefault("project_name", "alpha")
    return store.add_minutes(conn, title=title, body=kw.pop("body", "body"), author="ava", **kw)


def test_minutes_are_listed_newest_first_and_scoped_by_project(conn):
    _add(conn, "first")
    _add(conn, "second")
    _add(conn, "other", project_name="beta")
    assert [m["title"] for m in store.list_minutes(conn, project_name="alpha")] == [
        "second",
        "first",
    ]


def test_a_revision_keeps_the_guid_and_the_original(conn):
    first = _add(conn, "v1", session_id="s1")
    second = _add(conn, "v2", revises=first["guid"])
    assert (second["guid"], second["rev"]) == (first["guid"], 2)
    assert second["session_id"] == "s1"  # inherited
    assert [m["title"] for m in store.list_minutes(conn)] == ["v2"]  # newest revision only
    got = store.get_minutes(conn, first["guid"])
    assert got is not None
    assert (got["title"], got["revisions"]) == ("v2", [1, 2])
    old = store.get_minutes(conn, first["guid"], rev=1)
    assert old is not None
    assert old["title"] == "v1"  # the original is still there


def test_revising_an_unknown_guid_is_refused(conn):
    with pytest.raises(ValueError, match="no minutes"):
        _add(conn, revises="nope")


def test_get_by_row_id(conn):
    row = _add(conn, "by id")
    got = store.get_minutes(conn, str(row["id"]))
    assert got is not None
    assert got["title"] == "by id"
    assert store.get_minutes(conn, "999999") is None


def test_filter_by_session_and_since(conn):
    _add(conn, "mine", session_id="s1")
    _add(conn, "theirs", session_id="s2")
    assert [m["title"] for m in store.list_minutes(conn, session_id="s1")] == ["mine"]
    assert store.list_minutes(conn, since=9_999_999_999.0) == []


def test_mcp_tools_record_and_read_back(conn, monkeypatch):
    store.register_session(
        conn, "A", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=111
    )
    store.set_handle(conn, "A", "ava")
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: 111)
    out = srv.record_minutes("Auth work", "## Done\n- x", decision_ids=[3, 4], tags="auth")
    assert out["ok"] is True
    assert out["rev"] == 1
    rows = srv.read_minutes()
    assert [r["title"] for r in rows] == ["Auth work"]
    assert rows[0]["author"] == "ava"
    assert rows[0]["session_id"] == "A"
    assert rows[0]["decision_ids"] == "3,4"
    full = srv.read_minutes(ref=out["guid"])
    assert full[0]["body"] == "## Done\n- x"
    revised = srv.record_minutes("Auth work, final", "## Done\n- x\n- y", revises=out["guid"])
    assert revised["rev"] == 2
    assert srv.record_minutes("x", "y", revises="nope")["ok"] is False
    project_level = srv.record_minutes("Retro", "z", project_level=True)
    assert project_level["ok"] is True
    assert {r["session_id"] for r in srv.read_minutes()} == {"A", None}


def test_read_minutes_cuts_a_long_body_to_a_preview(conn, monkeypatch):
    store.register_session(
        conn, "A", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=111
    )
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: 111)
    srv.record_minutes("long", "x" * 1000)
    assert len(srv.read_minutes()[0]["body"]) < 300
