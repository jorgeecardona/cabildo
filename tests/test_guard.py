"""PreToolUse guard: deny force/delete push, tree-destroying git, mutating git in
a shared checkout, and (opt-in) commits on a protected trunk. File writes are no
longer gated per-file — agents coordinate by isolating in their own worktrees.
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
# File writes are no longer gated — coordination is by worktree, not file lease
# --------------------------------------------------------------------------- #
def test_edit_and_write_are_not_gated_even_with_a_peer(conn):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _edit(conn, "a", f"{ALPHA}/src/x.py") is None
    assert _edit(conn, "a", f"{ALPHA}/src/x.py", tool="Write") is None
    assert _edit(conn, "a", f"{ALPHA}/n.ipynb", tool="NotebookEdit") is None


def test_apply_patch_is_not_gated(conn):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    patch = "*** Begin Patch\n*** Update File: src/x.py\n*** End Patch"
    assert guard.deny_reason(conn, "a", "apply_patch", {"command": patch}) is None


# The claims store itself stays (the `claim` MCP tool and the daemon's conflict
# hints still use it); these exercise store/actions helpers, not the guard.
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


# --------------------------------------------------------------------------- #
# Destructive git — discards uncommitted work; denied whether or not alone
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

# Path-scoped discards and quoted mentions that must never read as destructive;
# tested ALONE, so the shared-checkout rule stays silent and only the destructive
# guard is under test.
NOT_DESTRUCTIVE = [
    "git status",
    "git stash list",
    "git stash pop",
    "git restore --staged src/x.py",
    "git restore --staged .",  # unstaging the tree leaves the working tree alone
    'echo "git reset --hard" >> notes.txt',
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
    'git log --oneline | grep "git clean -f"',  # mention past a real pipe
    'echo "git checkout ."',
]


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_destructive_git_denied_even_when_alone(conn, clock, command):
    # Denied by default whether or not a peer is here: the op discards work the
    # agent cannot bring back, dangerous even to a lone agent's own tree.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", command)
    assert reason is not None, command
    assert "uncommitted work" in reason or "opt out" in reason


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_destructive_git_still_denied_with_a_peer(conn, clock, command):
    # A peer sharing the tree does not change the verdict: the discard is
    # irrecoverable on its own, so the destructive guard fires before (and
    # instead of) the shared-checkout rule.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is not None, command


def test_destructive_git_off_mode_bypasses_the_guard(conn, clock, home):
    # A project opts out of the destructive guard via config: `[destructive].mode
    # = off`. Alone, nothing else fires, so the op then passes.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git reset --hard") is not None  # baseline: denied
    (home / "config.toml").write_text('[project.alpha]\ndestructive = { mode = "off" }\n')
    assert _bash(conn, "a", "git reset --hard") is None  # opted out


@pytest.mark.parametrize("command", NOT_DESTRUCTIVE)
def test_not_destructive_never_denied_when_alone(conn, clock, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None, command


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
# Force / delete push — destroys committed work on the remote
# --------------------------------------------------------------------------- #
FORCE_PUSH = [
    "git push --force origin main",
    "git push -f origin main",
    "git push -fv origin main",  # bundled short flags
    "git push --force-with-lease origin main",
    "git push --force-with-lease=main origin main",
    "git push --delete origin topic",
    "git push -d origin topic",
    "git push origin +main",  # force refspec, no colon
    "git push origin +refs/heads/main:refs/heads/main",
    "cd sub && git push --force origin main",  # found past a chain separator
]


@pytest.mark.parametrize("command", FORCE_PUSH)
def test_force_push_denied_even_when_alone(conn, clock, command):
    # Denied by default whether or not a peer is here: it discards committed work
    # on the remote that no fetch+rebase can bring back.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", command)
    assert reason is not None, command
    assert "remote" in reason and "fetch" in reason


def test_force_push_off_mode_bypasses_the_guard(conn, clock, home):
    # A force-push is the remote-side member of the destructive family, so the
    # same `[destructive].mode = off` switch opts out of it.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git push --force origin main") is not None  # baseline
    (home / "config.toml").write_text('[project.alpha]\ndestructive = { mode = "off" }\n')
    assert _bash(conn, "a", "git push --force origin main") is None  # opted out


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",  # plain push is not a force-push
        "git push origin HEAD:main",
        'git commit -m "use --force carefully"',  # the flag in a message
    ],
)
def test_non_force_push_not_caught_by_force_guard(conn, clock, command):
    # Alone, so the plain-push guard stays silent too: only the force guard is
    # under test here, and it must not fire on any of these.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None, command


# --------------------------------------------------------------------------- #
# Push-failure recipe — coach the rejected push instead of pre-blocking it
# --------------------------------------------------------------------------- #
REJECTION = (
    " ! [rejected]        main -> main (non-fast-forward)\n"
    "error: failed to push some refs to 'origin'\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n"
    "hint: its remote counterpart. Integrate the remote changes (e.g. 'git pull ...')."
)


def test_recipe_fires_on_a_rejected_push():
    recipe = guard.push_failure_recipe("git push origin main", REJECTION)
    assert recipe is not None
    assert "git fetch origin" in recipe
    assert "git rebase origin/main" in recipe  # target parsed from the command
    assert "reset --hard" in recipe and "force" in recipe  # names what NOT to do
    assert "<branch>" not in recipe


def test_recipe_parses_a_head_colon_refspec():
    recipe = guard.push_failure_recipe("git push origin HEAD:release", REJECTION)
    assert recipe is not None and "git rebase origin/release" in recipe


def test_recipe_uses_a_placeholder_when_the_target_is_unknown():
    recipe = guard.push_failure_recipe("git push", REJECTION)
    assert recipe is not None and "git rebase origin/<branch>" in recipe


def test_recipe_silent_on_a_clean_push():
    assert guard.push_failure_recipe("git push origin main", "Everything up-to-date") is None


def test_recipe_silent_on_a_non_push():
    assert guard.push_failure_recipe("git commit -m x", REJECTION) is None


def test_recipe_silent_without_output():
    assert guard.push_failure_recipe("git push origin main", None) is None


# --------------------------------------------------------------------------- #
# Committing on a protected trunk — diverges local main from origin (opt-in)
# --------------------------------------------------------------------------- #
@pytest.fixture
def trunk_guard(home):
    """Opt the `alpha` project into the trunk guard, which is off by default."""
    (home / "config.toml").write_text('[project.alpha]\ntrunk = { mode = "guard" }\n')
    return home


def test_commit_on_main_allowed_by_default(conn, tmp_path):
    # The trunk guard is opt-in: with no config, a plain commit on main — the
    # ordinary trunk-based flow — is allowed.
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit -m x") is None


def test_commit_on_main_is_denied(conn, tmp_path, trunk_guard):
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    reason = _bash(conn, "a", 'git commit -m "fix: whatever"')
    assert reason is not None
    assert "`main`" in reason
    assert "request(action=" in reason  # names the way out: push a lane to origin
    assert "git switch -c" in reason or "worktree add" in reason


def test_commit_on_master_is_denied(conn, tmp_path, trunk_guard):
    repo = _git_repo(tmp_path / "repo", branch="master")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit -m x") is not None


def test_commit_on_a_lane_is_allowed(conn, tmp_path, trunk_guard):
    repo = _git_repo(tmp_path / "repo", branch="feature-x")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit -m x") is None
    assert _bash(conn, "a", "git commit -am x") is None
    assert _bash(conn, "a", "git commit --amend --no-edit") is None


def test_commit_into_a_lane_from_a_main_rooted_session_is_allowed(conn, tmp_path, trunk_guard):
    # The bug: the guard judged by where the SESSION was launched (the main
    # checkout, on main), so a commit targeting a lane worktree was blocked. It
    # must judge by where the commit actually runs (its -C / a lone cd).
    main = _git_repo(tmp_path / "main", branch="main")
    lane = _git_repo(tmp_path / "lane", branch="feature-x")
    store.register_session(
        conn, "a", cwd=str(main), project=str(main), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", f"git -C {lane} commit -m x") is None  # -C targets the lane
    assert _bash(conn, "a", f"cd {lane} && git commit -m x") is None  # cd targets the lane
    assert _bash(conn, "a", "git commit -m x") is not None  # plain commit runs in main → denied


def test_commit_dash_c_onto_another_main_checkout_is_denied(conn, tmp_path, trunk_guard):
    # -C must not be a loophole: targeting another checkout that is itself on main
    # is still committing on trunk.
    main = _git_repo(tmp_path / "main", branch="main")
    other = _git_repo(tmp_path / "other", branch="main")
    store.register_session(
        conn, "a", cwd=str(main), project=str(main), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", f"git -C {other} commit -m x") is not None


def test_dry_run_commit_on_main_is_allowed(conn, tmp_path, trunk_guard):
    # --dry-run writes nothing, so it does not strand the branch.
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit --dry-run") is None


def test_amend_on_main_is_denied(conn, tmp_path, trunk_guard):
    # --amend still rewrites the trunk tip, so it diverges just the same.
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit --amend --no-edit") is not None


def test_trunk_off_mode_bypasses_the_guard(conn, tmp_path, home, trunk_guard):
    repo = _git_repo(tmp_path / "repo", branch="main")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    assert _bash(conn, "a", "git commit -m x") is not None  # baseline: opted into guard
    (home / "config.toml").write_text('[project.alpha]\ntrunk = { mode = "off" }\n')
    assert _bash(conn, "a", "git commit -m x") is None  # opted back out


def test_trunk_names_override(conn, tmp_path, home):
    # A repo whose trunk is called `trunk` protects it, and stops protecting main.
    repo = _git_repo(tmp_path / "repo", branch="trunk")
    store.register_session(
        conn, "a", cwd=str(repo), project=str(repo), project_name="alpha", claude_pid=1000
    )
    (home / "config.toml").write_text(
        '[project.alpha]\ntrunk = { mode = "guard", names = ["trunk"] }\n'
    )
    assert _bash(conn, "a", "git commit -m x") is not None  # `trunk` protected
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    assert _bash(conn, "a", "git commit -m x") is None  # main no longer in the set


def test_commit_off_a_repo_falls_through(conn, trunk_guard):
    # No real repo at the cwd: the branch can't be read, so the guard must not
    # block every commit — it falls through.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git commit -m x") is None


# --------------------------------------------------------------------------- #
# Mutating git in a shared checkout — the collapse of blanket-stage, branch
# switch, and the peers side of destructive into one rule: get your own worktree
# --------------------------------------------------------------------------- #
# Ops that reach the OTHER agents' work — staging/discarding the whole tree or
# moving HEAD/the branch — are denied in a shared checkout whatever the trunk
# mode (#35).
SHARED_DANGEROUS = [
    "git add -A",
    "git add .",
    "git add ./",  # trivially equal to `add .`
    "git add ..",  # escapes the subtree
    "git add :/",  # git pathspec magic: the whole tree
    "git add -u",
    "git commit -am x",  # -a sweeps every tracked change, a peer's included
    "git commit -m x .",  # a sweeping pathspec commits a peer's unstaged files
    "git commit -m x -- .",
    "git commit --amend --no-edit",  # rewrites the tip — can orphan a peer's commit
    "git checkout -b feature",
    "git checkout main",
    "git switch -c feature",
    "git branch -f main HEAD",  # force-moving a checked-out label
    "git merge origin/main",
    "git rebase main",
    "git pull",  # fetch + merge/rebase: a HEAD move under peers
    "git restore src/x.py",  # a discard, even when path-scoped
    "git stash apply",
    "git rm -r ./",  # removes every tracked file
    "git -C /repo/alpha add -A",
]

# Ops confined to your own explicit paths — the trunk-off flow of committing your
# own files on the shared branch. Denied under `[trunk].mode = "guard"` (lane
# workflow), allowed under trunk-off (the default).
SHARED_OWN_PATHS = [
    "git add src/x.py",
    "git commit -m x",
    "git mv a b",
    "git rm x",
    "cd /repo/alpha && git commit -m x",
]

SHARED_READONLY = [
    "git status",
    "git diff",
    "git log --oneline",
    "git branch",  # listing, not -f
    "git checkout",  # bare: prints
    "git switch",  # bare: prints
    "git commit --dry-run",
    "git stash list",
    "git stash show",
    "git fetch origin",
    "git remote -v",
]


@pytest.mark.parametrize("command", SHARED_DANGEROUS)
def test_shared_dangerous_git_denied_when_a_peer_shares_the_checkout(conn, command):
    # Default (trunk off): ops that reach a peer's work are still denied.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", command)
    assert reason is not None, command
    assert "@bob" in reason or "other agent" in reason
    assert "worktree add" in reason  # names the way out


def test_glued_separator_does_not_hide_a_dangerous_op(conn):
    # #35 review: `;`/`&&` glued to a token must still split, or an allowed `add`
    # would smuggle a denied op (reset/checkout) past the guard in a shared tree.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git add x; git reset --hard") is not None
    assert _bash(conn, "a", "git add x;git checkout main") is not None
    assert _bash(conn, "a", "git add file.py && git commit -m x") is None  # both own-paths


def test_commit_message_that_looks_like_a_flag_is_not_misread(conn):
    # `-m "add -A ..."` must not be read as `-a`/`-A`; a plain commit stays allowed.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", 'git commit -m "add -A support"') is None
    assert _bash(conn, "a", "git commit -mWIP") is None  # glued message, not `-m` + `-W`…


@pytest.mark.parametrize("command", SHARED_OWN_PATHS)
def test_own_paths_git_allowed_in_a_shared_checkout_under_trunk_off(conn, command):
    # #35: with trunk mode off (the default), committing your own explicit paths
    # on the shared branch is the intended flow — it must NOT be pushed to a
    # worktree.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None, command


@pytest.mark.parametrize("command", SHARED_OWN_PATHS)
def test_own_paths_git_denied_in_a_shared_checkout_under_trunk_guard(conn, command, trunk_guard):
    # Under the lane workflow every mutating op belongs in the agent's own tree,
    # so even a path-scoped add/commit in the shared checkout is denied.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    reason = _bash(conn, "a", command)
    assert reason is not None, command
    assert "worktree add" in reason


@pytest.mark.parametrize("command", SHARED_DANGEROUS + SHARED_OWN_PATHS)
def test_mutating_git_allowed_when_alone(conn, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None, command


def _shared_repo(conn, tmp_path):
    """Two sessions standing in one real repo with one commit."""
    repo = _git_repo(tmp_path / "repo")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    (repo / "f.txt").write_text("one\n")
    subprocess.run([*git, "add", "f.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    for sid, handle in (("a", "alice"), ("b", "bob")):
        make_session(conn, sid, handle=handle, project=str(repo), project_name="repo")
    return repo


@pytest.mark.parametrize(
    "command", ["git reset --soft HEAD~1", "git rebase main", "git merge main", "git pull"]
)
def test_tip_move_is_allowed_when_no_tracked_file_has_uncommitted_changes(conn, tmp_path, command):
    # #55: a peer's presence is not a conflict. With a clean tracked tree nobody has files at stake.
    _shared_repo(conn, tmp_path)
    assert _bash(conn, "a", command) is None


def test_tip_move_is_denied_when_a_tracked_file_is_modified(conn, tmp_path):
    repo = _shared_repo(conn, tmp_path)
    (repo / "f.txt").write_text("a peer is editing this\n")
    assert _bash(conn, "a", "git reset --soft HEAD~1") is not None


def test_tip_move_ignores_untracked_files(conn, tmp_path):
    repo = _shared_repo(conn, tmp_path)
    (repo / "scratch.txt").write_text("x\n")
    assert _bash(conn, "a", "git rebase main") is None


@pytest.mark.parametrize(
    "command", ["git checkout -b x", "git switch main", "git branch -f main HEAD"]
)
def test_branch_switch_stays_denied_on_a_clean_tree(conn, tmp_path, command):
    # It changes the branch a peer's next commit lands on, whatever the tree holds.
    _shared_repo(conn, tmp_path)
    assert _bash(conn, "a", command) is not None


def test_tip_move_is_denied_when_status_cannot_be_read(conn):
    # /repo/alpha does not exist: the check fails, so the guard falls back to deny.
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", "git reset --soft HEAD~1") is not None


def test_a_peer_that_moved_out_stops_sharing_once_its_hook_reports_the_new_cwd(conn, tmp_path):
    # #54: the stored cwd is refreshed by the tool hooks, so a stale row cannot block forever.
    repo = _shared_repo(conn, tmp_path)
    (repo / "f.txt").write_text("dirty\n")  # so the tip move needs the peer to be gone
    assert _bash(conn, "a", "git rebase main") is not None
    store.touch(conn, "b", cwd=str(tmp_path))
    assert _bash(conn, "a", "git rebase main") is None


def _main_and_worktree(conn, tmp_path):
    """Two sessions in one checkout, plus a linked worktree of it that nobody else stands in."""
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    wt = tmp_path / "wt-alice"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/alice\n")
    for sid, handle in (("a", "alice"), ("b", "bob")):
        store.register_session(
            conn, sid, cwd=str(main), project=str(main), project_name="alpha", claude_pid=1000
        )
        store.set_handle(conn, sid, handle)
        store.mark_wrote(conn, sid)
    return main, wt


def test_a_git_op_is_judged_by_the_tree_it_acts_on_not_the_session_folder(conn, tmp_path):
    # #29: the session started in the shared checkout and runs git in its own worktree.
    main, wt = _main_and_worktree(conn, tmp_path)
    assert _bash(conn, "a", f"git -C {wt} add -A") is None
    assert _bash(conn, "a", f"cd {wt} && git add -A") is None
    assert _bash(conn, "a", f"git -C {main} add -A") is not None  # the shared tree: still denied
    assert _bash(conn, "a", "git add -A") is not None


@pytest.mark.parametrize("command", SHARED_READONLY)
def test_readonly_git_allowed_even_with_a_peer(conn, command):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha")
    assert _bash(conn, "a", command) is None, command


def test_mutating_git_allowed_in_your_own_worktree(conn, tmp_path):
    """A worktree has its own index, tree, and HEAD, so a peer standing in one is
    not a sharer — this is the escape hatch the deny points at, so it must
    actually be allowed.
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

    assert _bash(conn, "a", "git commit -am x") is None
    assert _bash(conn, "b", "git commit -am x") is None


def wrote_at(conn, session_id):
    row = store.get_session(conn, session_id)
    assert row is not None
    return row["wrote_at"]


# --- #56: a peer that only reads does not block --------------------------------------------


def test_a_peer_that_never_wrote_does_not_block_a_dangerous_git_op(conn):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha", wrote=False)
    assert _bash(conn, "a", "git add -A") is None
    assert _bash(conn, "a", "git checkout -b x") is None


def test_a_peer_blocks_only_from_its_first_write(conn):
    make_session(conn, "a", handle="alice", project=ALPHA, project_name="alpha")
    make_session(conn, "b", handle="bob", project=ALPHA, project_name="alpha", wrote=False)
    assert _bash(conn, "a", "git add -A") is None
    store.mark_wrote(conn, "b")
    reason = _bash(conn, "a", "git add -A")
    assert reason is not None
    assert "who wrote here (@bob)" in reason


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        ("Edit", {"file_path": "/x"}, True),
        ("Write", {"file_path": "/x"}, True),
        ("NotebookEdit", {}, True),
        ("Read", {"file_path": "/x"}, False),
        ("Grep", {"pattern": "x"}, False),
        ("Bash", {"command": "git log --oneline -5"}, False),
        ("Bash", {"command": "git status && git diff"}, False),
        ("Bash", {"command": "ls -la | grep foo"}, False),
        ("Bash", {"command": "cat a.txt 2>&1"}, False),
        ("Bash", {"command": "make test >/dev/null 2>&1"}, False),
        ("Bash", {"command": "git commit -m x"}, True),
        ("Bash", {"command": "git add f.txt"}, True),
        ("Bash", {"command": "echo hi > out.txt"}, True),
        ("Bash", {"command": "echo hi >> out.txt"}, True),
        ("Bash", {"command": "sed -i 's/a/b/' f.py"}, True),
        ("Bash", {"command": "rm -rf build"}, True),
        ("Bash", {"command": "cat x | tee y"}, True),
    ],
)
def test_writes_files_tells_a_reader_from_a_writer(tool, tool_input, expected):
    assert guard.writes_files(tool, tool_input) is expected


def test_the_post_tool_hook_marks_a_session_as_a_writer(conn, monkeypatch):
    from cabildo.hooks import post_tool_use

    make_session(conn, "w", handle="writer", project=ALPHA, project_name="alpha", wrote=False)
    assert wrote_at(conn, "w") is None
    payload = {"session_id": "w", "tool_name": "Read", "tool_input": {"file_path": "/x"}}
    monkeypatch.setattr(post_tool_use, "read_input", lambda: payload)
    post_tool_use.main()
    assert wrote_at(conn, "w") is None
    payload.update(tool_name="Edit")
    post_tool_use.main()
    assert wrote_at(conn, "w") is not None
