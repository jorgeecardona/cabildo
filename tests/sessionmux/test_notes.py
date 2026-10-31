"""notes.py: the notes typed in the panel, shown on the row and edited like tags."""

from __future__ import annotations

from pathlib import Path

from cabildo import store
from cabildo.sessionmux import notes, sessions
from cabildo.sessionmux.app import SwitcherApp
from cabildo.sessionmux.notes import Note


def test_split_trims_and_drops_blank_parts() -> None:
    assert notes.split(" ask retry ;; fix  ; ") == ("ask retry", "fix")


def test_save_adds_keeps_and_removes_by_text() -> None:
    assert notes.save((), "s1", "one; two")
    [one, two] = notes.load(["s1"])["s1"]
    assert (one.text, two.text) == ("one", "two")

    assert notes.save((one, two), "s1", "two; three")
    after = notes.load(["s1"])["s1"]
    assert [n.text for n in after] == ["two", "three"]
    assert after[0].id == two.id  # a note still in the line keeps its id
    with store.connect() as conn:
        assert store.list_notes(conn, state="done")[0]["text"] == "one"  # a removed one is done


def test_save_marks_notes_human_and_the_banner_style_agent_notes_stay_out() -> None:
    with store.connect() as conn:
        store.add_note(conn, text="agent loose end", session_id="s1")
    notes.save((), "s1", "mine")
    assert [n.text for n in notes.load(["s1"])["s1"]] == ["mine"]


def test_load_is_empty_without_a_db_and_never_creates_it(bus_home: Path) -> None:
    assert notes.load(["s1"]) == {}
    assert not bus_home.exists()


def test_the_panel_row_shows_the_notes_after_the_tags(transcript) -> None:
    from lines import user

    transcript("s1", user("Fix the login bug"))
    notes.save((), "s1", "ask about retry; check logs")
    [row] = sessions.attach_notes(sessions.build_rows(None))
    assert row.notes[0] == Note(row.notes[0].id, "ask about retry")
    first = SwitcherApp._session_text(row, 100).plain.split("\n")[0]
    assert first.endswith("✎ ask about retry; check logs")
