"""sessions_focus: ask a running workspace to show one conversation."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest
from lines import PROJECT, ai_title, user
from pilot import drive, settle
from textual.widgets import DataTable

from cabildo import names, sessions_ops, store
from cabildo.workspace import focus, sessions
from cabildo.workspace.app import SwitcherApp
from conftest import Transcript


def test_ask_reaches_every_live_workspace_and_collects_the_replies() -> None:
    folder = focus.register()

    def workspace() -> None:
        while (request := focus.take(folder)) is None:
            pass
        focus.answer(folder, request, ok=True, message=f"showing {request.name}")

    t = threading.Thread(target=workspace)
    t.start()
    replies = focus.ask("tiny-dynamo", timeout=3)
    t.join()
    assert [(r.ok, r.message, r.pid) for r in replies] == [
        (True, "showing tiny-dynamo", os.getpid())
    ]
    focus.unregister(folder)
    assert focus.ask("x", timeout=0.2) == []


def test_ask_with_no_workspace_returns_nothing_and_drops_dead_folders() -> None:
    dead = focus.root() / "999999999"
    dead.mkdir(parents=True)
    assert focus.ask("x", timeout=0.2) == []
    assert not dead.exists()


def test_a_workspace_that_does_not_answer_is_left_out_and_its_request_removed() -> None:
    folder = focus.register()
    assert focus.ask("x", timeout=0.3) == []
    assert not (folder / "request.json").exists()
    focus.unregister(folder)


def test_find_prefers_an_exact_name_to_a_part_of_a_title(transcript: Transcript, bus_home) -> None:
    transcript("aaaa1111", user("one"), ai_title("Trip to Lisbon"), mtime=100)
    transcript("bbbb2222", user("two"), ai_title("Lisbon again"), mtime=200)
    conn = store.connect()
    store.register_session(conn, "bbbb2222", cwd=PROJECT, project=PROJECT, project_name="p")
    store.set_handle(conn, "bbbb2222", "again")
    conn.close()
    rows = sessions.attach_rooms(sessions.build_rows(PROJECT))
    ids = lambda name: sorted(r.head.session_id for r in sessions_ops.find(rows, name))
    assert ids("lisbon") == ["aaaa1111", "bbbb2222"]  # ambiguous: both titles
    assert ids("@again") == ["bbbb2222"]
    assert ids(names.robot_name("aaaa1111")) == ["aaaa1111"]
    assert ids("trip to") == ["aaaa1111"]
    assert ids("zzz") == []


def _ask(name: str) -> asyncio.Future[list[focus.Reply]]:
    return asyncio.get_running_loop().run_in_executor(None, focus.ask, name, 5.0)


def test_the_workspace_opens_the_named_conversation_and_answers(
    transcript: Transcript, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript("aaaa1111", user("one"), ai_title("Trip"), mtime=200)
    transcript("bbbb2222", user("two"), ai_title("Bug"), mtime=100)
    opened: list[str] = []

    async def fake_open(self: SwitcherApp, row, focus: bool = True) -> None:
        opened.append(row.head.session_id)

    monkeypatch.setattr(SwitcherApp, "_open", fake_open)
    got: dict[str, list[focus.Reply]] = {}

    async def scenario(app: SwitcherApp, pilot) -> None:
        got["bug"] = await _ask("bug")
        got["none"] = await _ask("nothing like it")
        await settle(app, pilot)
        got["cursor"] = [app.rows[app.query_one(DataTable).cursor_row].head.session_id]

    drive(monkeypatch, scenario)
    assert opened == ["bbbb2222"]
    assert [r.ok for r in got["bug"]] == [True] and "Bug" in got["bug"][0].message
    assert [r.ok for r in got["none"]] == [False]
    assert "no session" in got["none"][0].message
    assert got["cursor"] == ["bbbb2222"]
