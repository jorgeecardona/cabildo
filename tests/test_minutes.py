"""Minutas: distilling a readable conversation record from a Claude transcript, and
writing the per-project day file. Works on a crafted transcript, no real session.
"""

from __future__ import annotations

import json

import pytest

from cabildo import minutes, transcript


def _make(root, slug, session_id, lines):
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    # Claude writes compact JSON; the scanner's cheap gate matches `"type":"user"`
    # with no spaces, so dump the same way.
    p.write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")
    return p


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(transcript, "transcript_root", lambda: root)
    return root


def test_session_minutes_captures_title_turns_and_where_left_off(transcripts):
    p = _make(
        transcripts,
        "proj-x",
        "abc123",
        [_user("do the thing"), _assistant("on it"), _user("and then this")],
    )
    m = minutes.session_minutes(p)
    assert m is not None
    assert m["title"] == "do the thing"
    assert m["last"] == "and then this"
    text = minutes.render_session(m)
    assert "do the thing" in text and "on it" in text and "Left off" in text


def test_write_session_writes_the_day_file_and_is_idempotent(home, transcripts):
    slug = transcript.project_slug("/repo/x")
    _make(transcripts, slug, "sess1", [_user("hello there"), _assistant("hi")])
    out = minutes.write_session("sess1", "/repo/x", "x", day="2026-09-16")
    assert out is not None and out.exists()
    body = out.read_text()
    assert "Minutas — x — 2026-09-16" in body and "hello there" in body
    minutes.write_session("sess1", "/repo/x", "x", day="2026-09-16")  # again
    assert out.read_text().count("hello there") == body.count("hello there")  # not duplicated


def test_write_session_is_none_without_a_transcript(home, transcripts):
    assert minutes.write_session("no-such", "/repo/x", "x") is None
