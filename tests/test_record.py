"""Recording conversation turns from transcripts into the bus DB.

The ingest is opt-in and daemon-driven; these tests exercise the parts without a
daemon: the config gate, the transcript reader (uuid/model/cursor), the dedup
insert, and one end-to-end ingest against a fake transcript tree.
"""

from __future__ import annotations

import json

import pytest

from cabildo import config, record, store, transcript

pytestmark = pytest.mark.skipif(
    not hasattr(transcript, "session_messages"),
    reason="record backend absent from the installed build; CI verifies it",
)


def _line(obj: dict) -> str:
    # Compact, like a real Claude transcript: session_messages gates on the
    # substring '"type":"user"', which a spaced json.dumps would not contain.
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _user(uid: str, text: str, ts: str = "2026-09-17T10:00:00.000Z") -> str:
    return _line(
        {"type": "user", "uuid": uid, "timestamp": ts, "message": {"role": "user", "content": text}}
    )


def _assistant(
    uid: str, text: str, model: str = "claude-opus-4-8", ts: str = "2026-09-17T10:00:01.000Z"
) -> str:
    return _line(
        {
            "type": "assistant",
            "uuid": uid,
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
            },
        }
    )


# --- config gate -------------------------------------------------------------- #
def test_record_messages_defaults_off(home):
    assert config.record_messages("/anywhere") is False


def test_record_messages_reads_global_config(home):
    (home / "config.toml").write_text("[record]\nmessages = true\n")
    assert config.record_messages("/anywhere") is True


# --- transcript reader -------------------------------------------------------- #
def test_session_messages_parses_uuid_role_model(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_user("u1", "hello") + _assistant("a1", "hi there"))
    rows, offset = transcript.session_messages(f)

    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert [r["uuid"] for r in rows] == ["u1", "a1"]
    assert rows[0]["model"] is None and rows[1]["model"] == "claude-opus-4-8"
    assert rows[0]["text"] == "hello" and rows[1]["text"] == "hi there"
    assert all(isinstance(r["ts"], float) for r in rows)
    assert offset == f.stat().st_size


def test_session_messages_cursor_only_reads_the_tail(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_user("u1", "one"))
    _, off = transcript.session_messages(f)
    with f.open("a") as fh:
        fh.write(_assistant("a1", "two"))
    rows, off2 = transcript.session_messages(f, off)
    assert [r["uuid"] for r in rows] == ["a1"]
    assert off2 == f.stat().st_size


def test_session_messages_holds_a_partial_final_line(tmp_path):
    f = tmp_path / "s.jsonl"
    # A line still being written (no trailing newline) must not be read yet.
    f.write_text(_user("u1", "done") + '{"type":"assistant","uuid":"a1"')
    rows, off = transcript.session_messages(f)
    assert [r["uuid"] for r in rows] == ["u1"]
    assert off == len(_user("u1", "done").encode())  # cursor sits before the partial line


# --- dedup insert ------------------------------------------------------------- #
def test_record_transcript_messages_dedups_on_uuid(conn):
    rows = [
        {"uuid": "u1", "role": "user", "text": "hi", "ts": 1.0, "model": None},
        {"uuid": "a1", "role": "assistant", "text": "yo", "ts": 2.0, "model": "m"},
    ]
    n1 = store.record_transcript_messages(conn, "sess", rows, project_name="alpha")
    n2 = store.record_transcript_messages(conn, "sess", rows, project_name="alpha")
    assert n1 == 2 and n2 == 0
    total = conn.execute("SELECT count(*) c FROM transcript_messages").fetchone()["c"]
    assert total == 2


# --- end to end --------------------------------------------------------------- #
@pytest.fixture
def transcript_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(transcript, "transcript_root", lambda: root)
    return root


def _write_transcript(root, project: str, session_id: str, body: str):
    d = root / transcript.project_slug(project)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text(body)


def test_ingest_records_opted_in_sessions(conn, home, transcript_root):
    (home / "config.toml").write_text("[record]\nmessages = true\n")
    store.register_session(
        conn, "sess1", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=1
    )
    _write_transcript(
        transcript_root, "/repo/alpha", "sess1", _user("u1", "hi") + _assistant("a1", "yo")
    )

    n = record.ingest(conn)
    assert n == 2
    got = conn.execute(
        "SELECT role, body, project_name FROM transcript_messages ORDER BY rowid"
    ).fetchall()
    assert [r["role"] for r in got] == ["user", "assistant"]
    assert got[0]["project_name"] == "alpha"

    # Second pass: cursor is at EOF, nothing new.
    assert record.ingest(conn) == 0

    # Appended turn is picked up on the next pass.
    d = transcript_root / transcript.project_slug("/repo/alpha")
    with (d / "sess1.jsonl").open("a") as fh:
        fh.write(_user("u2", "more"))
    assert record.ingest(conn) == 1


def test_ingest_skips_when_project_not_opted_in(conn, home, transcript_root):
    # No [record] messages in config → default off → nothing recorded.
    store.register_session(
        conn, "sess1", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=1
    )
    _write_transcript(transcript_root, "/repo/alpha", "sess1", _user("u1", "hi"))
    assert record.ingest(conn) == 0
    assert conn.execute("SELECT count(*) c FROM transcript_messages").fetchone()["c"] == 0


def test_ingest_skips_non_claude_clients(conn, home, transcript_root):
    (home / "config.toml").write_text("[record]\nmessages = true\n")
    store.register_session(
        conn,
        "sess1",
        cwd="/repo/alpha",
        project="/repo/alpha",
        project_name="alpha",
        client="codex",
        owner_pid=1,
    )
    _write_transcript(transcript_root, "/repo/alpha", "sess1", _user("u1", "hi"))
    # Codex transcripts are a different format; this Claude reader must not touch them.
    assert record.ingest(conn) == 0
