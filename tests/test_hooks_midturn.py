"""Mid-turn hook behavior: PostToolUse delivery (once per message) and the
PreToolUse claims guard. Tests exercise the importable logic in store/notify/
guard, not the hook subprocesses — those are plumbing-only by design."""

from __future__ import annotations

from cabildo import guard, notify, store

from tests.conftest import make_session


def _urgent_dm(conn, frm, to, body="drop everything"):
    return store.post_message(
        conn, from_session=frm, from_handle=None, body=body,
        to_session=to, topic=None, project=None, urgent=True,
    )


# --------------------------------------------------------------------------- #
# Delivery (PostToolUse)
# --------------------------------------------------------------------------- #
def test_urgent_message_is_delivered_exactly_once(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _urgent_dm(conn, "a" * 8, "b" * 8)

    text = notify.midturn_urgent_text(conn, "b" * 8)
    assert text is not None and "URGENT" in text

    # Same unread message, next tool call: silence, not a re-injection.
    assert notify.midturn_urgent_text(conn, "b" * 8) is None


def test_non_urgent_mail_never_interrupts_a_turn(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    store.post_message(
        conn, from_session="a" * 8, from_handle=None, body="fyi",
        to_session="b" * 8, topic=None, project=None, urgent=False,
    )
    assert notify.midturn_urgent_text(conn, "b" * 8) is None


def test_delivery_does_not_mark_read(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _urgent_dm(conn, "a" * 8, "b" * 8)
    notify.midturn_urgent_text(conn, "b" * 8)
    unread = store.inbox(conn, "b" * 8, unread_only=True)
    assert len(unread) == 1


def test_already_read_urgent_is_not_delivered(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    mid = _urgent_dm(conn, "a" * 8, "b" * 8)
    store.mark_read(conn, "b" * 8, message_ids=[mid])
    assert notify.midturn_urgent_text(conn, "b" * 8) is None


# --------------------------------------------------------------------------- #
# Claims guard (PreToolUse)
# --------------------------------------------------------------------------- #
def _claim(conn, holder, resource, *, ttl=600, project="/repo/alpha"):
    r = store.acquire_claim(
        conn, resource=resource, holder_session=holder, holder_handle=None,
        project=project, ttl_seconds=ttl, note="wip",
    )
    assert r["ok"]


def test_push_denied_while_someone_else_holds_the_lock(conn):
    make_session(conn, "a" * 8, handle="holder")
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha")

    reason = guard.deny_reason(conn, "b" * 8, "Bash",
                               {"command": "git push origin main"})
    assert reason is not None and "@holder" in reason and "push:alpha" in reason


def test_holder_may_push_and_other_commands_pass(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha")

    assert guard.deny_reason(conn, "a" * 8, "Bash",
                             {"command": "git push"}) is None
    assert guard.deny_reason(conn, "b" * 8, "Bash",
                             {"command": "git status && git commit -m x"}) is None
    # `push` as data, not as a git subcommand:
    assert guard.deny_reason(conn, "b" * 8, "Bash",
                             {"command": "echo 'git push is guarded'"}) is None


def test_push_after_separator_is_caught(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha")
    reason = guard.deny_reason(conn, "b" * 8, "Bash",
                               {"command": "git commit -m x && git push -f"})
    assert reason is not None


def test_expired_lock_does_not_block(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha", ttl=-1)
    assert guard.deny_reason(conn, "b" * 8, "Bash",
                             {"command": "git push"}) is None


def test_file_claim_blocks_edit_including_globs(conn):
    make_session(conn, "a" * 8, handle="holder")
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "file:src/core.py")
    _claim(conn, "a" * 8, "file:hooks/*")

    deny = guard.deny_reason(conn, "b" * 8, "Edit",
                             {"file_path": "/repo/alpha/src/core.py"})
    assert deny is not None and "@holder" in deny

    deny = guard.deny_reason(conn, "b" * 8, "Write",
                             {"file_path": "/repo/alpha/hooks/stop.py"})
    assert deny is not None

    assert guard.deny_reason(conn, "b" * 8, "Edit",
                             {"file_path": "/repo/alpha/README.md"}) is None
    # The holder edits their own claimed file freely.
    assert guard.deny_reason(conn, "a" * 8, "Edit",
                             {"file_path": "/repo/alpha/src/core.py"}) is None


def test_file_claim_from_another_project_does_not_block(conn):
    make_session(conn, "a" * 8, project="/repo/beta", project_name="beta")
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "file:src/core.py", project="/repo/beta")
    assert guard.deny_reason(conn, "b" * 8, "Edit",
                             {"file_path": "/repo/alpha/src/core.py"}) is None
