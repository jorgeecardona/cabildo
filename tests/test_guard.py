"""PreToolUse guard: auto-claim on edit, file-lease enforcement (Edit/Write and
shell writes alike), and denial of tree-destroying git ops by default.
"""

from __future__ import annotations

import subprocess

import pytest

from cabildo import guard, store
from tests.conftest import make_session


def _git_repo(path, branch="main"):
    """A real git repo at ``path`` whose HEAD is an (unborn) ``branch`` — enough
    for ``git symbolic-ref --short HEAD`` to report the branch, no commit needed.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True
    )
    return path


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


def test_glob_claim_locks_matching_files(conn, clock):
    """A held glob claim (file:src/*.py) denies a peer editing any match, and
    leaves non-matching files free.
    """
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    store.acquire_claim(
        conn,
        resource="file:src/*.py",
        holder_session="a",
        holder_handle="alice",
        project=ALPHA,
        ttl_seconds=300,
        note=None,
    )
    denied = _edit(conn, "b", f"{ALPHA}/src/x.py")
    assert denied is not None and "@alice" in denied
    assert _edit(conn, "b", f"{ALPHA}/README.md") is None  # outside the glob


def test_directory_prefix_claim_locks_the_subtree(conn, clock):
    """A claim ending in / covers the whole subtree, which a bare fnmatch misses."""
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    store.acquire_claim(
        conn,
        resource="file:src/",
        holder_session="a",
        holder_handle="alice",
        project=ALPHA,
        ttl_seconds=300,
        note=None,
    )
    assert _edit(conn, "b", f"{ALPHA}/src/deep/y.py") is not None  # under the prefix
    assert _edit(conn, "b", f"{ALPHA}/src/x.py") is not None
    assert _edit(conn, "b", f"{ALPHA}/lib/z.py") is None  # sibling dir is free


def test_expired_glob_claim_stops_denying(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    store.acquire_claim(
        conn,
        resource="file:src/",
        holder_session="a",
        holder_handle="alice",
        project=ALPHA,
        ttl_seconds=300,
        note=None,
    )
    assert _edit(conn, "b", f"{ALPHA}/src/x.py") is not None
    clock["t"] += 301  # lease lapses
    assert _edit(conn, "b", f"{ALPHA}/src/x.py") is None  # auto-released


def test_file_pattern_matches_glob_prefix_exact():
    assert store.file_pattern_matches("src/", ["src/a/b.py"])  # directory prefix
    assert not store.file_pattern_matches("src/", ["lib/x.py"])
    assert store.file_pattern_matches("src/x.py", ["src/x.py"])  # exact
    assert store.file_pattern_matches("src/*.py", ["src/x.py"])  # glob


def test_file_claim_holders_reports_overlap(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    store.acquire_claim(
        conn,
        resource="file:src/",
        holder_session="a",
        holder_handle="alice",
        project=ALPHA,
        ttl_seconds=300,
        note=None,
    )
    holders = store.file_claim_holders(conn, ALPHA, ["src/x.py", "README.md"], exclude_session="b")
    assert len(holders) == 1
    assert holders[0]["holder"] == "@alice" and holders[0]["file"] == "src/x.py"
    # the pusher is excluded from its own conflict hint
    assert store.file_claim_holders(conn, ALPHA, ["src/x.py"], exclude_session="a") == []


def test_conflict_regex_extracts_the_files():
    from cabildo.actions import _CONFLICT_RE

    out = (
        "Auto-merging src/x.py\n"
        "CONFLICT (content): Merge conflict in src/x.py\n"
        "CONFLICT (add/add): Merge conflict in a/b.txt\n"
    )
    assert _CONFLICT_RE.findall(out) == ["src/x.py", "a/b.txt"]


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


def test_multiedit_is_gated_like_edit(conn, clock):
    # MultiEdit writes a file too; it must honor and take leases just like Edit.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    guard.autoclaim_on_edit(conn, "b", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    reason = _edit(conn, "a", f"{ALPHA}/src/x.py", tool="MultiEdit")
    assert reason is not None and "@bob" in reason


def test_multiedit_auto_claims_the_file(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _edit(conn, "a", f"{ALPHA}/src/x.py", tool="MultiEdit") is None
    guard.autoclaim_on_edit(conn, "a", "MultiEdit", {"file_path": f"{ALPHA}/src/x.py"})
    assert "@alice" in (_edit(conn, "b", f"{ALPHA}/src/x.py") or "")


# --------------------------------------------------------------------------- #
# Destructive git in a shared working tree
# --------------------------------------------------------------------------- #
DESTRUCTIVE = [
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git reset --hard origin/main",
    "git -C /repo reset --hard",  # git's own opt-with-value, then the subcommand
    "git checkout -- src/x.py",
    "git checkout HEAD -- src/x.py",
    "git checkout .",
    "git restore .",  # whole-tree discard
    "git restore :/",  # whole-tree pathspec
    "git clean -fd",
    "git clean -f",
    "git clean -xfd",
    "git stash",
    "git stash save wip",
    "git stash push",  # no pathspec: tree-wide
    "git stash push -m wip",  # message, still no pathspec: tree-wide
    "git stash drop",
    "git stash clear",
    "cd /repo && git reset --hard",  # destructive op is the second command
]

# Branch switching moved OUT of SAFE on 2026-08-03: it is safe alone and in your own worktree,
# and specifically unsafe in a checkout somebody else is standing in. See the tests at the bottom.
SAFE = [
    "git status",
    "git stash list",
    "git stash pop",
    "git restore --staged src/x.py",
    "git restore --staged .",  # unstaging the tree leaves the working tree alone
    'echo "git reset --hard" >> notes.txt',
    "git commit -m 'x'",
    "git stash show",
    "git stash apply",
    "git stash branch topic",
    "git diff",
    # issue #19: path-scoped discards cannot reach a peer's other files, and a
    # destructive word quoted inside a read-only command is not a git argv.
    "git restore src/x.py",
    "git restore -- src/x.py",
    "git restore -- a b c",
    "git stash push -- src/x.py",
    r'grep -rn "foo\|git stash\|bar" ./src',
    'git commit -m "reset --hard now"',  # destructive words are just the message
    'git log --oneline | grep "git clean -f"',  # mention past a real pipe
    'echo "git checkout ."',
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
def test_destructive_git_denied_even_when_alone(conn, clock, command):
    # Denied by default whether or not a peer is here: the op discards work the
    # agent cannot bring back, dangerous even to a lone agent's own tree.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", command)
    assert reason is not None, command
    assert "destructive" in reason.lower() or "opt out" in reason


def test_destructive_git_off_mode_bypasses_the_guard(conn, clock, home):
    # A project opts out of the destructive guard via config: `[destructive].mode
    # = off`. The guard then steps aside even with a live peer.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git reset --hard") is not None  # baseline: denied
    (home / "config.toml").write_text('[project.alpha]\ndestructive = { mode = "off" }\n')
    assert _bash(conn, "a", "git reset --hard") is None  # opted out


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
# Committing on a protected trunk — diverges local main from origin
# --------------------------------------------------------------------------- #
def test_commit_on_main_is_denied(conn, tmp_path):
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    reason = _bash(conn, "a", 'git commit -m "fix: whatever"')
    assert reason is not None
    assert "`main`" in reason
    assert "request(action=" in reason  # names the way out: land from a lane
    assert "git switch -c" in reason or "worktree add" in reason


def test_commit_on_master_is_denied(conn, tmp_path):
    repo = _git_repo(tmp_path / "repo", branch="master")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit -m x") is not None


def test_commit_on_a_lane_is_allowed(conn, tmp_path):
    repo = _git_repo(tmp_path / "repo", branch="feature-x")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit -m x") is None
    assert _bash(conn, "a", "git commit -am x") is None
    assert _bash(conn, "a", "git commit --amend --no-edit") is None


def test_dry_run_commit_on_main_is_allowed(conn, tmp_path):
    # --dry-run writes nothing, so it does not strand the branch.
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit --dry-run") is None


def test_amend_on_main_is_denied(conn, tmp_path):
    # --amend still rewrites the trunk tip, so it diverges just the same.
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit --amend --no-edit") is not None


def test_trunk_off_mode_bypasses_the_guard(conn, tmp_path, home):
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit -m x") is not None  # baseline: denied
    (home / "config.toml").write_text('[project.alpha]\ntrunk = { mode = "off" }\n')
    assert _bash(conn, "a", "git commit -m x") is None  # opted out


def test_trunk_names_override(conn, tmp_path, home):
    # A repo whose trunk is called `trunk` protects it, and stops protecting main.
    repo = _git_repo(tmp_path / "repo", branch="trunk")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    (home / "config.toml").write_text('[project.alpha]\ntrunk = { names = ["trunk"] }\n')
    assert _bash(conn, "a", "git commit -m x") is not None  # `trunk` protected
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    assert _bash(conn, "a", "git commit -m x") is None  # main no longer in the set


def test_commit_off_a_repo_falls_through(conn):
    # No real repo at the cwd: the branch can't be read, so the guard must not
    # block every commit — it falls through.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git commit -m x") is None


# --------------------------------------------------------------------------- #
# Unlocked push with live peers — routed to `push`
# --------------------------------------------------------------------------- #
def test_push_denied_when_peer_live_without_any_lock(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", "git push origin main")
    assert reason is not None
    assert "@bob" in reason
    assert "push" in reason  # names the ordered path through


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
    # the push to `push`.
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
    in crm an agent pushed three commits of unrelated work onto somebody else's feature branch.
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


# --------------------------------------------------------------------------- #
# Shell writes — the hole Edit/Write/apply_patch did not cover
# --------------------------------------------------------------------------- #
# Each of these writes src/x.py, which bob holds; none of them is an Edit/Write.
SHELL_WRITES = [
    "echo x > src/x.py",
    "echo x >src/x.py",
    "echo x >> src/x.py",
    "echo x >| src/x.py",
    "make build &> src/x.py",
    "cmd 2> src/x.py",
    "generate | tee src/x.py",
    "generate | tee -a src/x.py",
    "sed -i 's/a/b/' src/x.py",
    "sed -i -e 's/a/b/' src/x.py",
    "sed --in-place 's/a/b/' src/x.py",
    "dd if=/dev/zero of=src/x.py",
    "python -c \"open('src/x.py','w').write('h')\"",
    'python3 -c \'open("src/x.py", "a")\'',
]


@pytest.mark.parametrize("command", SHELL_WRITES)
def test_shell_write_to_a_held_file_is_denied(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    guard.autoclaim_on_edit(conn, "b", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    reason = _bash(conn, "a", command)
    assert reason is not None, command
    assert "@bob" in reason


# Writes that must NOT be mistaken for a redirect into src/x.py.
SHELL_NON_WRITES = [
    "cat src/x.py",  # read, not write
    "grep foo src/x.py",
    "cmd 2>&1 | grep src/x.py",  # descriptor dup, not a file redirect
    'python -c "print(1 > 0)"',  # `>` inside a quoted comparison
    "diff old.py src/x.py",
    "sed 's/a/b/' src/x.py",  # no -i: prints, does not rewrite
    "echo done > other.py",  # writes a different, unheld file
]


@pytest.mark.parametrize("command", SHELL_NON_WRITES)
def test_shell_non_write_is_not_denied(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    guard.autoclaim_on_edit(conn, "b", "Edit", {"file_path": f"{ALPHA}/src/x.py"})
    assert _bash(conn, "a", command) is None, command


def test_shell_write_auto_claims_the_file(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    # alice writes via the shell (allowed) -> auto-claims src/x.py
    assert _bash(conn, "a", "echo x > src/x.py") is None
    guard.autoclaim_on_edit(conn, "a", "Bash", {"command": "echo x > src/x.py"})
    # bob now reaches for the same file, by shell or by Edit, and is denied
    assert "@alice" in (_bash(conn, "b", "sed -i s/a/b/ src/x.py") or "")
    assert "@alice" in (_edit(conn, "b", f"{ALPHA}/src/x.py") or "")


def test_shell_write_without_targets_claims_nothing(conn, clock):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert guard.autoclaim_on_edit(conn, "a", "Bash", {"command": "ls -la"}) is None
    assert store.list_claims(conn, project=ALPHA) == []


@pytest.mark.parametrize(
    "command",
    [
        "cmd > /dev/null 2>&1",
        "make build > /tmp/build.log",
        "generate | tee /var/log/out",
    ],
)
def test_out_of_project_shell_write_is_not_auto_claimed(conn, clock, command):
    # A redirect to a throwaway path outside the project must not litter the
    # claims table (regression: /dev/null and /tmp/build.log were being claimed).
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert guard.autoclaim_on_edit(conn, "a", "Bash", {"command": command}) is None
    assert store.list_claims(conn) == []
