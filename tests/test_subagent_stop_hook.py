"""SubagentStop hook: a finished subagent posts a note to the project channel so
its work shows up in `cabildo watch`.
"""

from __future__ import annotations

import io
import json
import sys

from cabildo import store
from cabildo.hooks import subagent_stop


def _sub_transcript(tmp_path, task: str):
    t = tmp_path / "sub.jsonl"
    t.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-09-17T10:00:00Z",
                "message": {"role": "user", "content": task},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return t


def test_subagent_stop_posts_to_project_channel(conn, home, monkeypatch, tmp_path):
    store.register_session(
        conn, "parent", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=1
    )
    conn.commit()
    monkeypatch.setattr("cabildo.context.git_project", lambda cwd=None: ("/repo/alpha", "alpha"))
    payload = {
        "session_id": "parent",
        "cwd": "/repo/alpha",
        "transcript_path": str(_sub_transcript(tmp_path, "research the alternatives")),
        "hook_event_name": "SubagentStop",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    subagent_stop.main()

    rows = list(
        conn.execute("SELECT topic, from_session, body FROM messages WHERE topic='proj:alpha'")
    )
    assert len(rows) == 1
    assert rows[0]["from_session"] == "parent"
    assert "subagent finished" in rows[0]["body"]
    assert "research the alternatives" in rows[0]["body"]


def test_subagent_stop_without_a_project_is_silent(conn, home, monkeypatch, tmp_path):
    monkeypatch.setattr("cabildo.context.git_project", lambda cwd=None: ("/somewhere", ""))
    payload = {"session_id": "p", "cwd": "/somewhere", "hook_event_name": "SubagentStop"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    subagent_stop.main()
    assert conn.execute("SELECT count(*) c FROM messages").fetchone()["c"] == 0


def test_the_same_note_is_not_posted_twice_in_a_row(conn, home, monkeypatch, tmp_path):
    store.register_session(
        conn, "parent", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=1
    )
    conn.commit()
    monkeypatch.setattr("cabildo.context.git_project", lambda cwd=None: ("/repo/alpha", "alpha"))
    payload = {
        "session_id": "parent",
        "cwd": "/repo/alpha",
        "transcript_path": str(_sub_transcript(tmp_path, "same title")),
        "hook_event_name": "SubagentStop",
    }
    for _ in range(3):  # three subagents of one session finish
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        subagent_stop.main()
    assert (
        conn.execute("SELECT count(*) c FROM messages WHERE topic='proj:alpha'").fetchone()["c"]
        == 1
    )
