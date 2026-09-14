"""PreToolUse guard: auto-claim on edit, file-lease enforcement, and denial of
tree-destroying git ops when other agents share the working tree.
"""

from __future__ import annotations

import pytest

from cabildo import guard, store
from tests.conftest import make_session


@pytest.fixture
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
    assert res == "file:src/x.py"  # repo-relative
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
    clock["t"] += guard.AUTOCLAIM_TTL + 1  # alice moved on
    assert _edit(conn, "b", f"{ALPHA}/src/x.py") is None  # bob is free to take it


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

# Branch switching moved OUT of SAFE on 2026-08-03: it is safe alone and in your own worktree,
# and specifically unsafe in a checkout somebody else is standing in. See the tests at the bottom.
SAFE = [
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
    assert _bash(conn, "a", command) is None  # no peers -> no block


@pytest.mark.parametrize("command", SAFE)
def test_safe_git_never_denied(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None


def test_push_lock_still_enforced(conn, clock):
    # Regression: routing destructive-git alongside push must not break push.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    store.acquire_claim(
        conn,
        resource="push:alpha",
        holder_session="b",
        holder_handle="bob",
        project=ALPHA,
        ttl_seconds=600,
        note=None,
    )
    reason = _bash(conn, "a", "git push origin main")
    assert reason is not None and "push:alpha" in reason


# --------------------------------------------------------------------------- #
# Unlocked push with live peers — routed to `land`
# --------------------------------------------------------------------------- #
def test_push_denied_when_peer_live_without_any_lock(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", "git push origin main")
    assert reason is not None
    assert "@bob" in reason
    assert "land" in reason  # names the ordered path through


def test_push_allowed_when_alone(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git push origin main") is None


def test_holding_the_push_lock_is_the_escape_hatch(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    store.acquire_claim(
        conn,
        resource="push:alpha",
        holder_session="a",
        holder_handle="alice",
        project=ALPHA,
        ttl_seconds=600,
        note=None,
    )
    assert _bash(conn, "a", "git push origin main") is None  # alice may push
    assert _bash(conn, "b", "git push origin main") is not None  # bob may not


def test_push_manual_mode_bypasses_the_guard(conn, clock, home):
    # A project can opt out of the push guard via config: `[push].mode = manual`.
    # The guard then steps aside even with a live peer that would otherwise route
    # the push to `land`.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git push origin main") is not None  # baseline: denied
    (home / "config.toml").write_text('[project.alpha]\npush = { mode = "manual" }\n')
    assert _bash(conn, "a", "git push origin main") is None  # opted out


def test_push_denial_survives_an_expired_lease(conn, clock):
    # The lease lapsing must not turn into a free-for-all: peers are still here.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    store.acquire_claim(
        conn,
        resource="push:alpha",
        holder_session="a",
        holder_handle="alice",
        project=ALPHA,
        ttl_seconds=60,
        note=None,
    )
    clock["t"] += 61
    assert _bash(conn, "a", "git push origin main") is not None


# --------------------------------------------------------------------------- #
# Blanket staging in a shared checkout
# --------------------------------------------------------------------------- #
BLANKET = [
    "git add -A",
    "git add --all",
    "git add .",
    "git add -u",
    "git commit -a -m 'wip'",
    "git commit -am 'wip'",
    "git commit --all -m x",
    "cd /repo/alpha && git add -A && git commit -m x",
    "git -C /repo/alpha add -A",
]

TARGETED = [
    "git add src/x.py",
    "git add src/x.py tests/test_x.py",
    "git commit -m 'x'",
    "git commit --amend --no-edit",
    "git commit -m 'add -A support to the parser'",  # a flag inside a message
    "git status",
    "git diff",
]


@pytest.mark.parametrize("command", BLANKET)
def test_blanket_stage_denied_in_a_shared_tree(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", command)
    assert reason is not None
    assert "@bob" in reason


@pytest.mark.parametrize("command", TARGETED)
def test_targeted_staging_is_never_denied(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None


@pytest.mark.parametrize("command", BLANKET)
def test_blanket_stage_allowed_when_alone(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None


def test_blanket_stage_allowed_in_your_own_worktree(conn, clock, tmp_path):
    """The peer is on the same project but a different checkout: separate index,
    no collision, no deny. This is the behaviour that makes `git worktree add`
    an actual answer rather than advice.
    """
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    wt = tmp_path / "wt-bob"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/bob\n")

    store.register_session(
        conn, "a", cwd=str(main), project=str(main), project_name="alpha", claude_pid=1000
    )
    store.set_handle(conn, "a", "alice")
    store.register_session(
        conn, "b", cwd=str(wt), project=str(main), project_name="alpha", claude_pid=1001
    )
    store.set_handle(conn, "b", "bob")

    assert _bash(conn, "a", "git add -A") is None
    assert _bash(conn, "b", "git add -A") is None


# --------------------------------------------------------------------------- #
# Branch switching in a shared checkout — the hole that bit three repos
# --------------------------------------------------------------------------- #
def test_branch_switch_denied_when_another_agent_shares_the_checkout(conn):
    """A checkout has one HEAD. Switching it moves everyone standing in that directory, silently,
    and the next of them commits onto a branch it never chose. Happened three times on 2026-08-03;
    in crm an agent landed three commits of unrelated work onto somebody else's feature branch.
    """
    make_session(conn, "a", handle="mine", project="/repo/x", project_name="x")
    make_session(conn, "b", handle="other", project="/repo/x", project_name="x")

    for cmd in (
        "git checkout -b adopt-cabildo-issues",
        "git checkout main",
        "git switch -c feature",
        "git branch -f main HEAD",
    ):
        reason = guard.deny_reason(conn, "a", "Bash", {"command": cmd})
        assert reason and "other agent" in reason, cmd
        assert "worktree add" in reason  # name the way out, not just the refusal


def test_branch_switch_is_fine_when_alone(conn):
    make_session(conn, "solo", handle="solo", project="/repo/solo", project_name="solo")
    assert guard.deny_reason(conn, "solo", "Bash", {"command": "git checkout -b anything"}) is None


def test_branch_switch_is_fine_from_your_own_worktree(conn):
    """A worktree has its own HEAD, so switching it moves nobody. That is also the escape hatch the
    denial points at, so it had better actually be allowed.
    """
    make_session(conn, "a", handle="mine", project="/repo/y", project_name="y")
    make_session(conn, "wt", handle="peer", project="/repo/y", project_name="y")
    conn.execute("UPDATE sessions SET cwd=? WHERE session_id=?", ("/repo/y", "a"))
    conn.execute("UPDATE sessions SET cwd=? WHERE session_id=?", ("/repo/y-wt", "wt"))
    conn.commit()
    assert guard.deny_reason(conn, "a", "Bash", {"command": "git checkout -b x"}) is None


def test_reading_and_restoring_are_not_branch_switches(conn):
    """`git checkout -- file` is a discard with its own denial and its own reason; bare `checkout`
    prints. Neither should be reported as moving the branch.
    """
    make_session(conn, "a", handle="mine", project="/repo/x", project_name="x")
    make_session(conn, "b", handle="other", project="/repo/x", project_name="x")
    for cmd in ("git status", "git branch", "git checkout", "git log --oneline"):
        r = guard.deny_reason(conn, "a", "Bash", {"command": cmd})
        assert r is None or "moves the branch" not in r, cmd


# --------------------------------------------------------------------------- #
# Codex `apply_patch` — file leases across every path the envelope touches
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operation", ["Add File", "Update File", "Delete File", "Move to"])
def test_apply_patch_is_denied_when_it_touches_a_held_file(conn, clock, operation):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    # bob holds locked.py; alice's patch reaches for it under each operation kind
    guard.autoclaim_on_edit(conn, "b", "Edit", {"file_path": f"{ALPHA}/locked.py"})
    patch = f"*** Begin Patch\n*** Update File: free.py\n*** {operation}: locked.py\n*** End Patch"
    reason = guard.deny_reason(conn, "a", "apply_patch", {"command": patch})
    assert reason is not None
    assert "@bob" in reason


def test_apply_patch_auto_claims_every_path_including_both_move_ends(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    # `src/../a.py` normalizes to a.py; the move names both a.py (source) and b.py (dest)
    patch = "*** Begin Patch\n*** Update File: src/../a.py\n*** Move to: b.py\n*** End Patch"
    assert guard.deny_reason(conn, "a", "apply_patch", {"command": patch}) is None
    guard.autoclaim_on_edit(conn, "a", "apply_patch", {"command": patch})
    # both ends of the move are now alice's, so bob is denied on each
    for path in ("a.py", "b.py"):
        reason = guard.deny_reason(conn, "b", "Edit", {"file_path": f"{ALPHA}/{path}"})
        assert reason is not None and "@alice" in reason


def test_apply_patch_without_paths_is_a_no_op(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    empty = "*** Begin Patch\n*** End Patch"
    assert guard.deny_reason(conn, "a", "apply_patch", {"command": empty}) is None
    assert guard.autoclaim_on_edit(conn, "a", "apply_patch", {"command": empty}) is None
    assert store.list_claims(conn, project=ALPHA) == []
