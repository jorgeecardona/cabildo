"""Session notes (GitLab #34): loose ends scoped to the session that raised them.

The store rows, the MCP tools agents call (`note_add`/`notes`/`note_done`/
`note_park`), and the `cabildo notes` human console, all over one `session_notes`
table with a mutable `state` (open -> done / parked).
"""

from __future__ import annotations

import pytest

from cabildo import cli, store
from cabildo import mcp_server as srv

from .conftest import make_session


# --------------------------------------------------------------------------- #
# store.add_note / list_notes / set_note_state
# --------------------------------------------------------------------------- #
def test_add_note_is_session_scoped_and_open(conn):
    make_session(conn, "A", handle="alice")
    nid = store.add_note(conn, text="wire the doorbell", type="unfinished", session_id="A")
    rows = store.list_notes(conn, session_id="A")
    assert [r["id"] for r in rows] == [nid]
    assert rows[0]["state"] == "open" and rows[0]["type"] == "unfinished"


def test_list_notes_is_oldest_first_and_filters(conn):
    make_session(conn, "A")
    make_session(conn, "B")
    a1 = store.add_note(conn, text="first", session_id="A", project_name="alpha")
    a2 = store.add_note(conn, text="second", type="bug", session_id="A", project_name="alpha")
    store.add_note(conn, text="b-note", session_id="B", project_name="alpha")

    # one session's notes, in the order they arose
    assert [r["id"] for r in store.list_notes(conn, session_id="A")] == [a1, a2]
    # type filter
    assert [r["id"] for r in store.list_notes(conn, session_id="A", type="bug")] == [a2]
    # project-wide (all sessions) still oldest-first
    assert len(store.list_notes(conn, project_name="alpha")) == 3


def test_unknown_type_falls_back_to_topic(conn):
    make_session(conn, "A")
    nid = store.add_note(conn, text="x", type="nonsense", session_id="A")
    assert store.list_notes(conn, session_id="A")[0]["type"] == "topic"
    assert nid


def test_set_note_state_done_and_park(conn):
    make_session(conn, "A")
    nid = store.add_note(conn, text="split the gate", session_id="A")

    done = store.set_note_state(conn, nid, state="done")
    assert done is not None and done["state"] == "done"
    assert done["updated_at"] >= done["created_at"]

    parked = store.set_note_state(conn, nid, state="parked", parked_ref="42-slug")
    assert parked is not None and parked["state"] == "parked"
    assert parked["parked_ref"] == "42-slug"


def test_set_note_state_rejects_unknown_state_and_missing_id(conn):
    make_session(conn, "A")
    nid = store.add_note(conn, text="x", session_id="A")
    assert store.set_note_state(conn, nid, state="bogus") is None
    assert store.set_note_state(conn, 9999, state="done") is None
    # unchanged after the rejected writes
    assert store.list_notes(conn, session_id="A")[0]["state"] == "open"


def test_list_notes_state_filter(conn):
    make_session(conn, "A")
    keep = store.add_note(conn, text="keep", session_id="A")
    gone = store.add_note(conn, text="gone", session_id="A")
    store.set_note_state(conn, gone, state="done")
    assert [r["id"] for r in store.list_notes(conn, session_id="A", state="open")] == [keep]
    assert [r["id"] for r in store.list_notes(conn, session_id="A", state="done")] == [gone]


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #
@pytest.fixture
def two_agents(conn, monkeypatch):
    store.register_session(
        conn, "A", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=111
    )
    store.register_session(
        conn, "B", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=222
    )
    store.set_handle(conn, "A", "alice")
    store.set_handle(conn, "B", "bob")
    current = {"pid": 111}
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: current["pid"])
    return lambda pid: current.__setitem__("pid", pid)


def test_note_add_and_notes_are_scoped_to_the_calling_session(two_agents):
    two_agents(111)
    srv.note_add("alice's loose end", type="idea")
    two_agents(222)
    srv.note_add("bob's loose end")

    # alice sees only her own by default
    two_agents(111)
    mine = srv.notes()
    assert [n["text"] for n in mine] == ["alice's loose end"]
    assert mine[0]["type"] == "idea" and mine[0]["handle"] == "alice"

    # all_sessions widens to the project
    assert len(srv.notes(all_sessions=True)) == 2


def test_note_done_and_park_flow(two_agents):
    two_agents(111)
    nid = srv.note_add("finish the migration")["id"]
    assert srv.notes()  # open

    done = srv.note_done(nid)
    assert done["ok"] is True and done["note"]["state"] == "done"
    assert srv.notes() == []  # no longer open
    assert srv.notes(state="done")[0]["id"] == nid

    nid2 = srv.note_add("needs its own issue")["id"]
    parked = srv.note_park(nid2, ref="51-session-notes")
    assert parked["note"]["state"] == "parked"
    assert parked["note"]["parked_ref"] == "51-session-notes"


def test_note_done_on_a_missing_id_is_not_ok(two_agents):
    two_agents(111)
    assert srv.note_done(123456)["ok"] is False


# --------------------------------------------------------------------------- #
# `cabildo notes` CLI
# --------------------------------------------------------------------------- #
def test_cli_notes_lists_a_sessions_open_notes(conn, capsys):
    make_session(conn, "A", handle="alice")
    store.add_note(conn, text="open one", session_id="A")
    gone = store.add_note(conn, text="closed one", session_id="A")
    store.set_note_state(conn, gone, state="done")

    assert cli.main(["notes", "--session", "A"]) == 0
    out = capsys.readouterr().out
    assert "open one" in out
    assert "closed one" not in out  # default state filter is open


def test_cli_notes_triage_done_and_park(conn):
    make_session(conn, "A")
    nid = store.add_note(conn, text="triage me", session_id="A")
    assert cli.main(["notes", "--session", "A", "--done", str(nid)]) == 0
    assert store.list_notes(conn, session_id="A")[0]["state"] == "done"

    nid2 = store.add_note(conn, text="park me", session_id="A")
    assert cli.main(["notes", "--session", "A", "--park", str(nid2), "--ref", "60-x"]) == 0
    row = next(r for r in store.list_notes(conn, session_id="A") if r["id"] == nid2)
    assert row["state"] == "parked" and row["parked_ref"] == "60-x"


def test_cli_notes_unknown_note_refused(conn, capsys):
    make_session(conn, "A")
    assert cli.main(["notes", "--session", "A", "--done", "9999"]) == 1
    assert "no note" in capsys.readouterr().err


def test_cli_notes_unknown_session_is_refused(conn, capsys):
    make_session(conn, "A")
    assert cli.main(["notes", "--session", "ghost"]) == 1
    assert "no such session" in capsys.readouterr().err
