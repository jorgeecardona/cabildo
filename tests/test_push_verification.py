"""The verifier must be held to the standard it enforces.

push() exists to stop components reporting success they never earned, so its own
post-push claim is the one place in this system that must never be wrong — in
either direction. A false "pushed" ships unverified work; a false "I did NOT
push this" sends the requester an urgent DM about work that is safely on main,
and the obvious reaction to that DM (retry, re-push, re-rebase) is destructive.

These tests use real git against a real bare remote. No mocking of git itself:
the claim under test is "the remote actually has my commit", and a mocked remote
cannot falsify that.
"""

from __future__ import annotations

import subprocess

import pytest

from cabildo import actions

ENV_ID = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def sh(cmd, cwd, env=None):
    import os

    p = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **ENV_ID, **(env or {})},
    )
    assert p.returncode == 0, f"{cmd} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def on_remote(origin, sha, branch="main") -> bool:
    """Ground truth, asked of the remote itself: is sha on the branch at all —
    tip or not? This is the question push() is really answering.
    """
    # --git-dir, not cwd: environments that set safe.bareRepository=explicit
    # (Claude Code injects it via GIT_CONFIG_*) refuse git run from inside a
    # bare repo unless the git dir is named explicitly.
    return (
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(origin),
                "merge-base",
                "--is-ancestor",
                sha,
                f"refs/heads/{branch}",
            ],
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.fixture
def repo(tmp_path):
    """A bare origin at 'base', plus a clone holding one unpushed commit."""
    origin, seed, work = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    sh(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path)
    sh(["git", "init", "-b", "main", str(seed)], tmp_path)
    (seed / "f.txt").write_text("base\n")
    sh(["git", "add", "f.txt"], seed)
    sh(["git", "commit", "-m", "base"], seed)
    sh(["git", "push", str(origin), "main"], seed)

    sh(["git", "clone", str(origin), str(work)], tmp_path)
    (work / "mine.txt").write_text("my work\n")
    sh(["git", "add", "mine.txt"], work)
    sh(["git", "commit", "-m", "my work"], work)
    return origin, work, sh(["git", "rev-parse", "HEAD"], work)


def on_remote_file(origin, path, branch="main") -> bool:
    """Is ``path`` in the tip tree of ``branch`` on the remote?"""
    return (
        subprocess.run(
            ["git", "--git-dir", str(origin), "cat-file", "-e", f"refs/heads/{branch}:{path}"],
            capture_output=True,
        ).returncode
        == 0
    )


def race_on_receive(origin):
    """Make origin append a commit the moment it accepts a push — i.e. another
    agent (or Jorge's VS Code) pushing between our push and our verify.
    """
    hook = origin / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/bash\n"
        "export GIT_DIR=$(pwd)\n"
        "tip=$(git rev-parse main); tree=$(git rev-parse main^{tree})\n"
        "c=$(GIT_AUTHOR_NAME=o GIT_AUTHOR_EMAIL=o@x GIT_COMMITTER_NAME=o "
        'GIT_COMMITTER_EMAIL=o@x git commit-tree "$tree" -p "$tip" -m other)\n'
        'git update-ref refs/heads/main "$c"\n'
    )
    hook.chmod(0o755)


def push(work):
    ctx = actions.Ctx(worker="test", allow_push=True, log=lambda _m: None)
    return actions.push({"cwd": str(work), "target": "main"}, ctx)


def test_clean_push_is_reported_pushed(repo):
    origin, work, sha = repo
    res = push(work)
    assert on_remote(origin, sha), "precondition: the push really happened"
    assert res["ok"] and res["pushed"] is True
    assert sha.startswith(res["head"])


def _add_prepush(work, script="#!/bin/bash\nexit 1\n"):
    hook = work / ".git" / "hooks" / "pre-push"
    hook.write_text(script)
    hook.chmod(0o755)


def test_daemon_push_skips_the_repos_pre_push_hook(repo, home):
    """Issue #14: the daemon already ran the gate, so its push must not re-run the
    repo's pre-push hook (which duplicates the gate and, if slow, times the push
    out). A failing pre-push must NOT stop the push under the default.
    """
    origin, work, sha = repo
    _add_prepush(work)  # a pre-push that fails outright
    res = push(work)
    assert on_remote(origin, sha), "the default push skips the failing pre-push hook"
    assert res["ok"] and res["pushed"] is True


def test_dry_run_push_reports_dry_run_and_pushes_nothing(repo, home):
    """Issue #15: a dry run (no --allow-push) must not read as pushed. It carries
    an explicit status so the daemon reports `dry_run`, not `done`.
    """
    origin, work, sha = repo
    ctx = actions.Ctx(worker="t", allow_push=False, log=lambda _m: None)
    res = actions.push({"cwd": str(work), "target": "main"}, ctx)
    assert res["ok"] is True
    assert res["pushed"] is False
    assert res["status"] == "dry_run"
    assert not on_remote(origin, sha), "a dry run must push nothing"


def _add_provision(work, script):
    d = work / ".cabildo"
    d.mkdir(exist_ok=True)
    p = d / "provision.sh"
    p.write_text(script)
    p.chmod(0o755)


def test_failing_provision_is_reported_as_provisioning_not_gate(repo, home):
    """Issue #18: a fresh worktree's missing env is a provisioning failure, not a
    broken change, and must not read as a gate failure.
    """
    origin, work, sha = repo
    _add_provision(work, "#!/bin/bash\necho 'no .venv here' >&2\nexit 3\n")
    res = push(work)
    assert res["ok"] is False and not res["pushed"]
    assert "provisioning failed" in res["reason"].lower()
    assert not on_remote(origin, sha)


def test_provision_runs_before_the_gate(repo, home):
    origin, work, sha = repo
    # provision writes a marker; a gate that requires it proves the ordering.
    _add_provision(work, "#!/bin/bash\ntouch provisioned.marker\n")
    gate = work / ".cabildo" / "gate.sh"
    gate.write_text("#!/bin/bash\ntest -f provisioned.marker\n")
    gate.chmod(0o755)
    res = push(work)
    assert res["ok"] and res["pushed"] is True
    assert on_remote(origin, sha)


def test_push_verify_true_keeps_the_pre_push_hook(repo, home):
    """Opt back in: [push].verify = true runs the repo's pre-push hook on the
    daemon's push, so a failing hook blocks the push.
    """
    origin, work, sha = repo
    (work / ".cabildo").mkdir()
    (work / ".cabildo" / "config.toml").write_text("[push]\nverify = true\n")
    _add_prepush(work)
    res = push(work)
    assert not on_remote(origin, sha), "verify=true runs the failing hook, so nothing pushes"
    assert res["ok"] is False and not res["pushed"]


def test_push_is_not_reported_when_push_is_rejected(repo):
    """The other direction: nothing on the remote must never read as pushed."""
    origin, work, sha = repo
    # A pre-receive that refuses everything: the push fails for real.
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/bash\nexit 1\n")
    hook.chmod(0o755)
    res = push(work)
    assert not on_remote(origin, sha), "precondition: the push really was refused"
    assert res["ok"] is False and not res["pushed"]


def test_commit_pushed_under_a_concurrent_push_is_not_called_a_failure(repo):
    """THE RACE. Our commit reaches main; another pushes on top before we look.

    Equality against the branch TIP answers "am I the newest commit?", which is
    not the question. The question is "is my work on the branch?" — and it is.
    Reporting failure here is the verifier committing the exact sin it exists to
    catch, and it fires an urgent DM telling the requester to go fix a push that
    already succeeded.
    """
    origin, work, sha = repo
    race_on_receive(origin)
    res = push(work)
    assert on_remote(origin, sha), "precondition: our commit IS on origin/main"
    assert res["pushed"] is True, (
        f"push() disowned a commit that is on the remote: {res.get('reason')}"
    )
    assert res["ok"] is True


def test_unverifiable_push_is_not_reported_as_not_pushed(repo, monkeypatch):
    """'I could not check' is not 'I did not do it'.

    If ls-remote itself fails, the honest answer is that we do not know. Calling
    that a failed push is a claim the daemon cannot support — the same
    unearned-certainty bug, one level up, and it points the requester at exactly
    the wrong recovery.
    """
    origin, work, sha = repo
    real_git = actions.git

    def blind(args, cwd, *a, **k):
        if args and args[0] == "ls-remote":
            return 128, "fatal: unable to access remote (simulated)"
        return real_git(args, cwd, *a, **k)

    monkeypatch.setattr(actions, "git", blind)
    res = push(work)
    assert on_remote(origin, sha), "precondition: the push really did push"
    assert res["pushed"] is not False, (
        "claimed the push did not push when it could not see the remote at all"
    )
    assert "verif" in (res.get("reason") or "").lower(), (
        f"an unverifiable push must say so, got: {res.get('reason')!r}"
    )


# --------------------------------------------------------------------------- #
# What counts as "dirty"
#
# Refusing a dirty tree is right, but only tracked changes can reach a commit and
# therefore the push. Blocking on an untracked scratch file makes a correct
# refusal look arbitrary — and the obvious way past an arbitrary gate is to start
# deleting files.
# --------------------------------------------------------------------------- #
def test_untracked_files_do_not_block_a_push(repo):
    origin, work, sha = repo
    (work / "scratch.md").write_text("notes to self\n")
    (work / "build.log").write_text("noise\n")

    res = push(work)
    assert res["ok"] and res["pushed"] is True, (
        f"an untracked file blocked a push it cannot affect: {res.get('reason')}"
    )
    assert on_remote(origin, sha)
    assert (work / "scratch.md").exists(), "push must not clean the tree"
    status = next(s for s in res["steps"] if s["step"] == "status")
    assert "untracked" in status["out"], "the ignored files should be visible"


def test_modified_tracked_file_is_left_alone_and_the_push_runs_in_its_own_checkout(repo, home):
    """#50: a peer's unfinished work in the shared tree neither blocks the push nor reaches
    the gate. The daemon gates the committed HEAD in a checkout of its own.
    """
    origin, work, sha = repo
    (work / "f.txt").write_text("half-written\n")
    gate = work / ".cabildo" / "gate.sh"
    gate.parent.mkdir()
    # the gate fails if the half-written file is on disk where it runs
    gate.write_text("#!/bin/bash\n! grep -q half-written f.txt\n")
    gate.chmod(0o755)
    sh(["git", "add", ".cabildo/gate.sh"], work)
    sh(["git", "commit", "-m", "gate"], work)
    sha = sh(["git", "rev-parse", "HEAD"], work)
    (work / "f.txt").write_text("half-written\n")

    res = push(work)
    assert res["ok"] and res["pushed"] is True, res.get("reason")
    assert res["isolated"] is True
    assert on_remote(origin, sha)
    assert (work / "f.txt").read_text() == "half-written\n", "the requester's tree is untouched"
    assert "git pull --rebase" in res["note"]
    iso = next(s for s in res["steps"] if s["step"] == "isolate")
    assert "left alone" in iso["out"]
    assert "f.txt" in iso["out"]


def test_staged_but_uncommitted_change_is_also_left_alone(repo, home):
    origin, work, sha = repo
    (work / "new.txt").write_text("staged\n")
    sh(["git", "add", "new.txt"], work)
    res = push(work)
    assert res["ok"] and res["pushed"] is True
    assert on_remote(origin, sha)
    assert not on_remote_file(origin, "new.txt"), "uncommitted work must not be pushed"
    assert sh(["git", "status", "--porcelain"], work).startswith("A  new.txt")


def test_the_isolated_checkout_is_reused_by_the_next_push(repo, home):
    origin, work, sha = repo
    (work / "f.txt").write_text("wip\n")
    first = push(work)
    assert first["pushed"] is True
    (work / "second.txt").write_text("more\n")
    sh(["git", "add", "second.txt"], work)
    sh(["git", "commit", "-m", "second"], work)
    sha2 = sh(["git", "rev-parse", "HEAD"], work)
    second = push(work)
    assert second["pushed"] is True
    assert on_remote(origin, sha2)
    assert len(list((home / "gate-worktrees").iterdir())) == 1


def test_a_clean_tree_is_not_isolated(repo):
    _origin, work, _sha = repo
    res = push(work)
    assert res["pushed"] is True
    assert res["isolated"] is False
    assert res["note"] is None


# --------------------------------------------------------------------------- #
# Post-push local sync (issue #20): local `main` must not drift behind origin.
# --------------------------------------------------------------------------- #


def _push_cwd(cwd):
    ctx = actions.Ctx(worker="test", allow_push=True, log=lambda _m: None)
    return actions.push({"cwd": str(cwd), "target": "main"}, ctx)


def _set_local_sync(work, mode):
    d = work / ".cabildo"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(f'[push]\nlocal_sync = "{mode}"\n')


def _lane(work):
    """Put ``work`` on a lane branch holding the unpushed commit, with local
    ``main`` moved back to the remote base so a push has something to fast-forward.
    Returns the lane's committed sha.
    """
    sha = sh(["git", "rev-parse", "HEAD"], work)
    sh(["git", "checkout", "-b", "lane"], work)
    sh(["git", "branch", "-f", "main", "origin/main"], work)  # local main back to base
    return sha


def test_ff_only_advances_pristine_local_main(repo, home):
    """The default: after a lane push, local `main` fast-forwards to the pushed
    commit instead of drifting a commit behind (issue #20).
    """
    origin, work, _ = repo
    pushed = _lane(work)
    before = sh(["git", "rev-parse", "main"], work)
    assert before != pushed, "precondition: local main is behind the lane commit"

    res = _push_cwd(work)
    assert res["ok"] and res["pushed"] is True
    assert sh(["git", "rev-parse", "main"], work) == pushed, "local main did not advance"
    assert "fast-forward" in res["local_sync"].lower()


def test_ff_only_refuses_on_diverged_local_main_without_data_loss(repo, home):
    """A local `main` that has diverged is never clobbered under ff-only: the push
    still succeeds, but local `main` is left exactly as it was.
    """
    origin, work, _ = repo
    _lane(work)
    # Give local main a commit of its own that origin will never have.
    sh(["git", "checkout", "main"], work)
    (work / "local-only.txt").write_text("do not lose me\n")
    sh(["git", "add", "local-only.txt"], work)
    sh(["git", "commit", "-m", "local only"], work)
    diverged = sh(["git", "rev-parse", "main"], work)
    sh(["git", "checkout", "lane"], work)

    res = _push_cwd(work)
    assert res["ok"] and res["pushed"] is True, "a sync refusal must not fail the push"
    assert sh(["git", "rev-parse", "main"], work) == diverged, "diverged main was clobbered"
    assert "not synced" in res["local_sync"].lower()


def test_ff_only_warns_when_main_is_checked_out_elsewhere(repo, home):
    """Pushing from a linked lane worktree while `main` is checked out in the
    primary: fetch refuses to touch the checked-out branch, so the push warns and
    leaves it for the human — it never reaches into that tree (issue #20).
    """
    origin, work, _ = repo
    sh(["git", "push", str(origin), "main"], work)  # primary main == origin, clean
    base = sh(["git", "rev-parse", "main"], work)
    lane_wt = work.parent / "lane_wt"
    sh(["git", "worktree", "add", "-b", "lane", str(lane_wt)], work)
    (lane_wt / "lane.txt").write_text("lane work\n")
    sh(["git", "add", "lane.txt"], lane_wt)
    sh(["git", "commit", "-m", "lane work"], lane_wt)

    res = _push_cwd(lane_wt)
    assert res["ok"] and res["pushed"] is True
    assert sh(["git", "rev-parse", "main"], work) == base, "primary's main was touched"
    assert "not synced" in res["local_sync"].lower()


def test_reset_fires_only_when_configured(repo, home):
    """`reset` force-syncs a diverged local `main`; without it the default refuses.
    Same divergent setup, opposite outcome, gated purely by config.
    """
    origin, work, _ = repo
    _lane(work)
    sh(["git", "checkout", "main"], work)
    (work / "local-only.txt").write_text("clobber me\n")
    sh(["git", "add", "local-only.txt"], work)
    sh(["git", "commit", "-m", "local only"], work)
    sh(["git", "checkout", "lane"], work)
    _set_local_sync(work, "reset")

    res = _push_cwd(work)
    assert res["ok"] and res["pushed"] is True
    pushed = sh(["git", "rev-parse", "HEAD"], work)
    assert sh(["git", "rev-parse", "main"], work) == pushed, "reset did not hard-sync main"
    assert "hard-synced" in res["local_sync"].lower()


def test_off_leaves_local_main_untouched(repo, home):
    """`off` is a no-op: local `main` stays behind, today's behavior preserved."""
    origin, work, _ = repo
    pushed = _lane(work)
    _set_local_sync(work, "off")
    before = sh(["git", "rev-parse", "main"], work)

    res = _push_cwd(work)
    assert res["ok"] and res["pushed"] is True
    assert sh(["git", "rev-parse", "main"], work) == before != pushed, "off must not sync"
    assert "off" in res["local_sync"].lower()


def test_manual_push_mode_rejects_the_push(repo):
    """A repo on push.mode = 'manual' has opted out of cabildo's push path. The
    daemon must refuse a push that reaches it anyway (stale queue entry, or a
    hand-filed request), and refuse it before touching the tree or the remote.
    """
    origin, work, sha = repo
    cabildo_dir = work / ".cabildo"
    cabildo_dir.mkdir()
    (cabildo_dir / "config.toml").write_text('[push]\nmode = "manual"\n')

    with pytest.raises(actions.Reject) as excinfo:
        push(work)
    assert "manual" in str(excinfo.value)
    assert not on_remote(origin, sha), "nothing must have been pushed"


def test_result_says_the_pre_push_hook_was_skipped_when_the_repo_has_one(repo, home):
    """#32: `gate: passed` must not read as "everything the repo checks ran"."""
    origin, work, sha = repo
    _add_prepush(work)  # a hook that would fail, which --no-verify skips
    res = push(work)
    assert res["pushed"] is True
    assert res["pre_push"] == "skipped"


def test_result_says_none_when_the_repo_has_no_pre_push_hook(repo, home):
    origin, work, sha = repo
    res = push(work)
    assert res["pushed"] is True
    assert res["pre_push"] == "none"


def test_result_says_ran_when_verify_is_on(repo, home):
    origin, work, sha = repo
    (work / ".cabildo").mkdir(exist_ok=True)
    (work / ".cabildo" / "config.toml").write_text("[push]\nverify = true\n")
    res = push(work)
    assert res["pushed"] is True
    assert res["pre_push"] == "ran"
