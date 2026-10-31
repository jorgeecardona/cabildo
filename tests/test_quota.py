"""Out of tokens: the StopFailure hook marks a session, notes written with no model
call (``source=human``) are shown at the next SessionStart.
"""

from __future__ import annotations

import io
import json
import time

from cabildo import notify, store, transcript
from cabildo.hooks import stop_failure
from tests.conftest import make_session

REJECTED = {
    "type": "assistant",
    "error": "rate_limit",
    "quotaLimits": {"status": "rejected", "resetsAt": 1791601200, "rateLimitType": "seven_day"},
}


def _transcript(tmp_path, *records):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_last_quota_rejection_reads_kind_and_reset(tmp_path):
    path = _transcript(tmp_path, {"type": "user"}, REJECTED, {"type": "user"})
    assert transcript.last_quota_rejection(path) == {"kind": "seven_day", "resets_at": 1791601200}


def test_allowed_quota_record_is_not_a_rejection(tmp_path):
    path = _transcript(tmp_path, {"quotaLimits": {"status": "allowed"}})
    assert transcript.last_quota_rejection(path) is None


def test_missing_transcript_is_none(tmp_path):
    assert transcript.last_quota_rejection(tmp_path / "nope.jsonl") is None


def _fire(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    stop_failure.main()


def test_stop_failure_marks_session_out_until_reset(conn, tmp_path, monkeypatch):
    make_session(conn, "a" * 8)
    resets = time.time() + 3600
    path = _transcript(
        tmp_path, {**REJECTED, "quotaLimits": {**REJECTED["quotaLimits"], "resetsAt": resets}}
    )
    _fire(
        monkeypatch,
        {"session_id": "a" * 8, "error": "rate_limit", "transcript_path": str(path)},
    )
    assert store.quota_out(conn, "a" * 8) == {"kind": "seven_day", "resets_at": resets}


def test_other_errors_do_not_mark_out(conn, monkeypatch):
    make_session(conn, "a" * 8)
    _fire(monkeypatch, {"session_id": "a" * 8, "error": "overloaded"})
    assert store.quota_out(conn, "a" * 8) is None


def test_rate_limit_without_transcript_is_out_with_unknown_reset(conn, monkeypatch):
    make_session(conn, "a" * 8)
    _fire(monkeypatch, {"session_id": "a" * 8, "error": "rate_limit"})
    assert store.quota_out(conn, "a" * 8) == {"kind": "unknown", "resets_at": 0.0}


def test_out_ends_when_reset_time_passes(conn):
    make_session(conn, "a" * 8)
    store.mark_out_of_tokens(conn, "a" * 8, kind="five_hour", resets_at=time.time() - 1)
    assert store.quota_out(conn, "a" * 8) is None


def test_completed_turn_clears_out(conn):
    make_session(conn, "a" * 8)
    store.mark_out_of_tokens(conn, "a" * 8, kind="five_hour", resets_at=0)
    store.clear_out_of_tokens(conn, "a" * 8)
    assert store.quota_out(conn, "a" * 8) is None


def test_human_notes_show_in_session_banner_agent_notes_do_not(conn):
    make_session(conn, "a" * 8)
    store.add_note(conn, text="ask about the retry", session_id="a" * 8, source="human")
    store.add_note(conn, text="agent loose end", session_id="a" * 8)
    banner = notify.session_banner(conn, "a" * 8)
    assert "ask about the retry" in banner
    assert "agent loose end" not in banner
