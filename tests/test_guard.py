"""PreToolUse guard: auto-claim on edit, file-lease enforcement, and denial of
tree-destroying git ops when other agents share the working tree."""

from __future__ import annotations

import pytest

from cabildo import guard, store
from tests.conftest import make_session


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": 1_000.0}
    monkeypatch.setattr(store.time, "time", lambda: state["t"])
    return state


ALPHA = "/repo/alpha"


def _edit(conn, sid, path, tool="Edit"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return guard.deny_reason(conn, sid, tool, {key: path})


def _bash(conn, sid, command):
    return guard.deny_reason(conn, sid, "Bash", {"command": command})


# --------------------------------------------------------------------------- #
# Auto-claim on edit — the teeth
# --------------------------------------------------------------------------- #
def test_editing_a_file_auto_claims_it(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    res = guard.autoclaim_on_edit(conn, "a", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    assert res == "file:src/x.py"                      # repo-relative
    claims = store.list_claims(conn, project=ALPHA)
    assert claims[0]["resource"] == "file:src/x.py"
    assert claims[0]["holder"] == "@alice"


def test_after_autoclaim_another_agent_is_denied(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    # alice edits (allowed) -> auto-claims
    assert _edit(conn, "a", f"{ALPHA}/src/x.py") is None
    guard.autoclaim_on_edit(conn, "a", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    # bob now reaches for the same file and is denied, told it's alice's
    reason = _edit(conn, "b", f"{ALPHA}/src/x.py")
    assert reason is not None
    assert "@alice" in reason


def test_holder_can_keep_editing_its_own_file(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    guard.autoclaim_on_edit(conn, "a", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    clock["t"] += 60
    # re-edit by the same agent is never denied, and refreshes the lease
    assert _edit(conn, "a", f"{ALPHA}/src/x.py") is None
    r2 = guard.autoclaim_on_edit(conn, "a", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    assert r2 == "file:src/x.py"
    assert len([c for c in store.list_claims(conn) if c["resource"] == "file:src/x.py"]) == 1


def test_autoclaim_lease_expires(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    guard.autoclaim_on_edit(conn, "a", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    clock["t"] += guard.AUTOCLAIM_TTL + 1          # alice moved on
    assert _edit(conn, "b", f"{ALPHA}/src/x.py") is None   # bob is free to take it


def test_autoclaim_absolute_path_outside_project(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    res = guard.autoclaim_on_edit(conn, "a", "Edit", {"file_path": "/etc/thing.py"})
    assert res == "file:/etc/thing.py"


def test_autoclaim_ignored_for_non_file_tools(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert guard.autoclaim_on_edit(conn, "a", "Bash", {"command": "ls"}) is None
    assert store.list_claims(conn) == []


# --------------------------------------------------------------------------- #
# Destructive git in a shared working tree
# --------------------------------------------------------------------------- #
DESTRUCTIVE = [
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git checkout -- src/x.py",
    "git checkout .",
    "git restore src/x.py",
    "git clean -fd",
    "git stash",
    "git stash push -m wip",
    "cd /repo && git reset --hard",
]

SAFE = [
    "git checkout -b feature",
    "git checkout main",
    "git status",
    "git stash list",
    "git stash pop",
    "git restore --staged src/x.py",
    'echo "git reset --hard" >> notes.txt',
    "git commit -m 'x'",
]


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_destructive_git_denied_when_peer_active(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", command)
    assert reason is not None
    assert "uncommitted work" in reason
    assert "@bob" in reason


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_destructive_git_allowed_when_alone(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None       # no peers -> no block


@pytest.mark.parametrize("command", SAFE)
def test_safe_git_never_denied(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None


def test_push_lock_still_enforced(conn, clock):
    # Regression: routing destructive-git alongside push must not break push.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    store.acquire_claim(conn, resource="push:alpha", holder_session="b",
                        holder_handle="bob", project=ALPHA, ttl_seconds=600, note=None)
    reason = _bash(conn, "a", "git push origin main")
    assert reason is not None and "push:alpha" in reason
