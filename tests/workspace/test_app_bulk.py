"""Selection and the bulk actions in the workspace: tags, export, combine, trash (#44)."""

from __future__ import annotations

from lines import ai_title, user
from pilot import drive, settle
from textual.widgets import Input

from cabildo import sessions_ops
from cabildo.workspace import sessions


def _three(transcript) -> None:
    transcript("aaaa1111", user("one"), ai_title("One"), mtime=300)
    transcript("bbbb2222", user("two"), ai_title("Two"), mtime=200)
    transcript("cccc3333", user("three"), ai_title("Three"), mtime=100)


async def _answer(app, pilot, text: str) -> None:
    app.screen.query_one(Input).value = text
    await pilot.press("enter")
    await settle(app, pilot)


def test_space_selects_and_t_tags_the_selection(transcript, monkeypatch) -> None:
    _three(transcript)

    async def scenario(app, pilot) -> None:
        await pilot.press("space", "space")  # selects One and Two, cursor moves down
        assert app.selected == {"aaaa1111", "bbbb2222"}
        await pilot.press("t")
        await _answer(app, pilot, "old, relationship")
        await pilot.press("t")
        await _answer(app, pilot, "-old")

    drive(monkeypatch, scenario)
    meta = sessions.load_meta()
    assert meta["aaaa1111"].tags == ("relationship",)
    assert meta["bbbb2222"].tags == ("relationship",)
    assert "cccc3333" not in meta


def test_e_exports_the_selection(transcript, monkeypatch) -> None:
    _three(transcript)
    out = sessions.data_dir() / "x.md"

    async def scenario(app, pilot) -> None:
        await pilot.press("space", "space", "e")
        await _answer(app, pilot, str(out))

    drive(monkeypatch, scenario)
    text = out.read_text()
    assert "one" in text
    assert "two" in text
    assert "three" not in text


def test_c_combines_into_a_new_pane_and_d_needs_it_started(
    transcript, monkeypatch, fake_claude
) -> None:
    _three(transcript)
    made: list[str] = []

    async def scenario(app, pilot) -> None:
        await pilot.press("space", "space", "c")
        await _answer(app, pilot, "yes")
        made.extend(sid for sid, m in sessions.load_meta().items() if m.combined_from)
        assert app.selected == set()
        await pilot.press("space", "space")  # the sources again (the new one has no row yet)
        await pilot.press("D")
        await settle(app, pilot)
        assert len(sessions_ops.rows(None)) == 3  # not started: nothing trashed

    drive(monkeypatch, scenario)
    assert len(made) == 1
    assert set(sessions.load_meta()[made[0]].combined_from) == {"aaaa1111", "bbbb2222"}
