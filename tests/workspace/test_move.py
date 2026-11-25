"""sessions_ops.move and the sessions_move tool: a conversation changes folder."""

from __future__ import annotations

import pytest
from lines import PROJECT, assistant, continued_in, user

from cabildo import mcp_server as srv
from cabildo import sessions_ops
from cabildo.workspace import agents, sessions


@pytest.fixture
def elsewhere(tmp_path):
    folder = tmp_path / "other"
    folder.mkdir()
    return folder


def test_move_puts_every_transcript_of_the_conversation_in_the_new_folder(
    transcript, elsewhere
) -> None:
    origin = transcript("origin", user("q"), assistant("a"), continued_in("cont"))
    cont = transcript("cont", user("q2"))
    beside = origin.with_suffix("")
    beside.mkdir()
    (beside / "subagent.jsonl").write_text("{}\n")
    (row,) = sessions.build_rows(PROJECT)
    moved = sessions_ops.move(row, str(elsewhere))
    dest = sessions.project_dir(str(elsewhere.resolve()))
    assert moved.files == 2 and set(moved.ids) == {"origin", "cont"}
    assert (dest / "origin.jsonl").exists() and (dest / "cont.jsonl").exists()
    assert (dest / "origin" / "subagent.jsonl").exists()
    assert not origin.exists() and not cont.exists() and not beside.exists()


def test_the_moved_conversation_is_listed_in_its_new_folder(transcript, elsewhere) -> None:
    transcript("s1", user("hello"), assistant("a"))
    (row,) = sessions.build_rows(PROJECT)
    sessions_ops.move(row, str(elsewhere))
    assert sessions.build_rows(PROJECT) == []
    (there,) = sessions.build_rows(str(elsewhere.resolve()))
    assert there.ids == ("s1",)


def test_move_refuses_a_missing_folder_and_the_folder_it_is_in(transcript, tmp_path) -> None:
    transcript("s1", user("q"))
    (row,) = sessions.build_rows(PROJECT)
    with pytest.raises(sessions_ops.SessionsError, match="no such directory"):
        sessions_ops.move(row, str(tmp_path / "nope"))
    here = tmp_path / "here"
    here.mkdir()
    sessions_ops.move(row, str(here))
    (moved,) = sessions.build_rows(str(here.resolve()))
    with pytest.raises(sessions_ops.SessionsError, match="already in"):
        sessions_ops.move(moved, str(here))


def test_move_does_not_overwrite_a_transcript_with_the_same_id(transcript, elsewhere) -> None:
    transcript("s1", user("q"))
    dest = sessions.project_dir(str(elsewhere.resolve()))
    dest.mkdir(parents=True)
    (dest / "s1.jsonl").write_text("{}\n")
    (row,) = sessions.build_rows(PROJECT)
    with pytest.raises(sessions_ops.SessionsError, match="already exists"):
        sessions_ops.move(row, str(elsewhere))


def test_the_tool_refuses_while_a_claude_runs_it(transcript, elsewhere, monkeypatch) -> None:
    transcript("s1", user("q"))
    running = agents.AgentState(
        session_id="s1",
        agent_id="s1",
        kind="interactive",
        name="n",
        cwd=PROJECT,
        status="idle",
        state="",
        pid=123,
    )
    monkeypatch.setattr(agents, "live_agents", lambda: {"s1": running})
    monkeypatch.setattr(srv, "_sessions_scope", lambda _all: ("A", PROJECT))
    out = srv.sessions_move("s1", str(elsewhere))
    assert out["ok"] is False and "running" in out["error"]
    assert sessions.build_rows(PROJECT)  # nothing moved


def test_the_tool_moves_and_reports_when_nothing_runs(transcript, elsewhere, monkeypatch) -> None:
    transcript("s1", user("q"))
    monkeypatch.setattr(agents, "live_agents", dict)
    monkeypatch.setattr(srv, "_sessions_scope", lambda _all: ("A", PROJECT))
    out = srv.sessions_move("s1", str(elsewhere))
    assert out == {"ok": True, "to": str(elsewhere), "files": 1, "ids": ["s1"]}
