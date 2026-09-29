"""Mid-turn hook behavior: PostToolUse delivery (once per message) and the
PreToolUse claims guard. Tests exercise the importable logic in store/notify/
guard, not the hook subprocesses — those are plumbing-only by design.
"""

from __future__ import annotations

from cabildo import guard, notify, store
from tests.conftest import make_session


def _urgent_dm(conn, frm, to, body="drop everything"):
    return store.post_message(
        conn,
        from_session=frm,
        from_handle=None,
        body=body,
        to_session=to,
        topic=None,
        project=None,
        urgent=True,
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
        conn,
        from_session="a" * 8,
        from_handle=None,
        body="fyi",
        to_session="b" * 8,
        topic=None,
        project=None,
        urgent=False,
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
        conn,
        resource=resource,
        holder_session=holder,
        holder_handle=None,
        project=project,
        ttl_seconds=ttl,
        note="wip",
    )
    assert r["ok"]


def test_push_denied_while_someone_else_holds_the_lock(conn):
    make_session(conn, "a" * 8, handle="holder")
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha")

    reason = guard.deny_reason(conn, "b" * 8, "Bash", {"command": "git push origin main"})
    assert reason is not None and "@holder" in reason and "push:alpha" in reason


def test_holder_may_push_and_other_commands_pass(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha")

    assert guard.deny_reason(conn, "a" * 8, "Bash", {"command": "git push"}) is None
    assert (
        guard.deny_reason(conn, "b" * 8, "Bash", {"command": "git status && git commit -m x"})
        is None
    )
    # `push` as data, not as a git subcommand:
    assert (
        guard.deny_reason(conn, "b" * 8, "Bash", {"command": "echo 'git push is guarded'"}) is None
    )


def test_push_after_separator_is_caught(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha")
    reason = guard.deny_reason(conn, "b" * 8, "Bash", {"command": "git commit -m x && git push -f"})
    assert reason is not None


def test_expired_lock_stops_being_the_reason(conn):
    """An expired lease no longer names its holder. The push is still refused
    while that holder is live on the project — but as a race with a peer, routed
    to `push`, not as somebody's lock.
    """
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha", ttl=-1)
    reason = guard.deny_reason(conn, "b" * 8, "Bash", {"command": "git push"})
    assert reason is not None
    assert "push:alpha" not in reason.split("must push by hand")[0]
    assert "push" in reason


def test_expired_lock_does_not_block_a_lone_agent(conn):
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "push:alpha", ttl=-1)
    store.end_session(conn, "a" * 8)  # holder gone, lease gone, nobody left
    assert guard.deny_reason(conn, "b" * 8, "Bash", {"command": "git push"}) is None


def test_file_claim_blocks_edit_including_globs(conn):
    make_session(conn, "a" * 8, handle="holder")
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "file:src/core.py")
    _claim(conn, "a" * 8, "file:hooks/*")

    deny = guard.deny_reason(conn, "b" * 8, "Edit", {"file_path": "/repo/alpha/src/core.py"})
    assert deny is not None and "@holder" in deny

    deny = guard.deny_reason(conn, "b" * 8, "Write", {"file_path": "/repo/alpha/hooks/stop.py"})
    assert deny is not None

    assert guard.deny_reason(conn, "b" * 8, "Edit", {"file_path": "/repo/alpha/README.md"}) is None
    # The holder edits their own claimed file freely.
    assert (
        guard.deny_reason(conn, "a" * 8, "Edit", {"file_path": "/repo/alpha/src/core.py"}) is None
    )


def test_file_claim_from_another_project_does_not_block(conn):
    make_session(conn, "a" * 8, project="/repo/beta", project_name="beta")
    make_session(conn, "b" * 8)
    _claim(conn, "a" * 8, "file:src/core.py", project="/repo/beta")
    assert (
        guard.deny_reason(conn, "b" * 8, "Edit", {"file_path": "/repo/alpha/src/core.py"}) is None
    )


# --------------------------------------------------------------------------- #
# Desktop notifications: whose message is it, and what does it say
# --------------------------------------------------------------------------- #
def test_only_messages_addressed_to_the_human_pop_a_notification(conn):
    """Measured, not assumed: of the last 40 desktop pops, ZERO were addressed to him. Twenty were
    the daemon telling an agent its push failed; twenty were agents coordinating on a project
    channel. `urgent` means "interrupt the recipient" — and when the recipient is another agent,
    this desk is not the recipient.
    """
    from cabildo import daemon

    make_session(conn, "agent-a", handle="alice", project="/repo/x", project_name="x")
    make_session(conn, "agent-b", handle="bob", project="/repo/x", project_name="x")
    store.post_message(
        conn,
        from_session="agent-a",
        from_handle="alice",
        body="urgent, for bob",
        to_session="agent-b",
        urgent=True,
    )
    store.post_message(
        conn,
        from_session="agent-a",
        from_handle="alice",
        body="urgent, to a channel",
        topic="proj:x",
        urgent=True,
    )
    assert daemon._pending_for_human(conn, 0) == []
    assert len(daemon._pending_for_human(conn, 0, everything=True)) == 2  # --notify-all

    store.post_message(
        conn,
        from_session="agent-a",
        from_handle="alice",
        body="this one is for you",
        to_session=store.HUMAN_SESSION,
    )
    got = daemon._pending_for_human(conn, 0)
    assert [m["body"] for m in got] == ["this one is for you"]


def test_a_notification_shows_the_verdict_not_the_git_status(monkeypatch):
    """The body is written for an agent's inbox, where a file list is right. A popup is a different
    medium: he is not at that terminal and cannot scroll.
    """
    from cabildo import desktop

    sent = {}
    monkeypatch.setattr(
        desktop, "notify", lambda s, b, **k: sent.update(summary=s, body=b, **k) or True
    )
    desktop.notify_message(
        {
            "from_display": "@daemon",
            "project_name": "crm",
            "urgent": True,
            "body": "❌ intent #28 `push` → failed working tree has uncommitted tracked changes — "
            "commit or stash first. The daemon pushes commits, it does not decide what:\n"
            "M apps/api/src/crm/config.py\nM apps/web/vite.config.ts\nM docker-compose.yml",
        }
    )
    assert sent["summary"] == "❌ crm"
    assert "commit or stash first." in sent["body"]
    assert "vite.config.ts" not in sent["body"]  # the dump is counted, not shown
    assert "3 file(s) listed" in sent["body"]
    # the daemon reporting on work it was asked to do must not hold the screen
    assert sent["urgent"] is False
