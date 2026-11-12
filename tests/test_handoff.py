"""Handoff notes: a deliberate note appended to a task's body, and the auto-note a
session leaves on any task it still held when it ended.
"""

from __future__ import annotations

from prodc import issue_store as issues

from cabildo import agenda, handoff, store
from tests.conftest import make_session


def _body(folder, slug):
    return issues.load(issues.resolve(folder, slug), agenda.config(folder)).body


def test_append_adds_a_dated_block_and_keeps_the_original_body(tmp_path):
    folder = agenda.ensure(tmp_path / "ag")
    agenda.add(folder, title="Task", tier="NOW", body="the original body")
    updated = handoff.append(folder, "task", "resume at the parser step", who="@alice")
    assert "the original body" in updated.body
    assert "## Handoff" in updated.body
    assert "resume at the parser step" in updated.body and "@alice" in updated.body
    assert "resume at the parser step" in _body(folder, "task")  # persisted


def test_two_handoffs_stack(tmp_path):
    folder = agenda.ensure(tmp_path / "ag")
    agenda.add(folder, title="Task", tier="NOW")
    handoff.append(folder, "task", "first note", who="@a")
    handoff.append(folder, "task", "second note", who="@b")
    body = _body(folder, "task")
    assert body.count("## Handoff") == 2
    assert "first note" in body and "second note" in body


def test_on_session_end_notes_every_held_task(conn):
    folder = agenda.ensure(agenda.global_dir())  # CABILDO_HOME/agenda (tmp)
    agenda.add(folder, title="Held task", tier="NOW")
    make_session(conn, "sx", handle="dev")
    store.acquire_claim(
        conn,
        resource="issue:held-task",
        holder_session="sx",
        holder_handle=None,
        project=None,
        ttl_seconds=600,
        note=None,
    )
    assert handoff.on_session_end(conn, "sx") == ["held-task"]
    body = _body(folder, "held-task")
    assert "## Handoff" in body and "@dev" in body and "Session ended" in body


def test_on_session_end_is_a_no_op_without_claims(conn):
    make_session(conn, "idle", handle="nobody")
    assert handoff.on_session_end(conn, "idle") == []


def test_locate_returns_none_when_the_slug_is_nowhere(conn):
    assert handoff.locate("no-such-slug", None) is None
