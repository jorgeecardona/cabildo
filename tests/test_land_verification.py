"""The verifier must be held to the standard it enforces.

land() exists to stop components reporting success they never earned, so its own
post-push claim is the one place in this system that must never be wrong — in
either direction. A false "landed" ships unverified work; a false "I did NOT
land this" sends the requester an urgent DM about work that is safely on main,
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
    tip or not? This is the question land() is really answering.
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


def race_on_receive(origin):
    """Make origin append a commit the moment it accepts a push — i.e. another
    agent (or Jorge's VS Code) landing between our push and our verify.
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


def land(work):
    ctx = actions.Ctx(worker="test", allow_push=True, log=lambda _m: None)
    return actions.land({"cwd": str(work), "target": "main"}, ctx)


def test_clean_land_is_reported_landed(repo):
    origin, work, sha = repo
    res = land(work)
    assert on_remote(origin, sha), "precondition: the push really happened"
    assert res["ok"] and res["landed"] is True
    assert sha.startswith(res["head"])


def _add_prepush(work, script="#!/bin/bash\nexit 1\n"):
    hook = work / ".git" / "hooks" / "pre-push"
    hook.write_text(script)
    hook.chmod(0o755)


def test_daemon_push_skips_the_repos_pre_push_hook(repo, home):
    """Issue #14: the daemon already ran the gate, so its push must not re-run the
    repo's pre-push hook (which duplicates the gate and, if slow, times the push
    out). A failing pre-push must NOT stop the land under the default.
    """
    origin, work, sha = repo
    _add_prepush(work)  # a pre-push that fails outright
    res = land(work)
    assert on_remote(origin, sha), "the default push skips the failing pre-push hook"
    assert res["ok"] and res["landed"] is True


def test_dry_run_land_reports_dry_run_and_pushes_nothing(repo, home):
    """Issue #15: a dry run (no --allow-push) must not read as landed. It carries
    an explicit status so the daemon reports `dry_run`, not `done`.
    """
    origin, work, sha = repo
    ctx = actions.Ctx(worker="t", allow_push=False, log=lambda _m: None)
    res = actions.land({"cwd": str(work), "target": "main"}, ctx)
    assert res["ok"] is True
    assert res["landed"] is False
    assert res["status"] == "dry_run"
    assert not on_remote(origin, sha), "a dry run must push nothing"


def test_push_verify_true_keeps_the_pre_push_hook(repo, home):
    """Opt back in: [push].verify = true runs the repo's pre-push hook on the
    daemon's push, so a failing hook blocks the land.
    """
    origin, work, sha = repo
    (work / ".cabildo").mkdir()
    (work / ".cabildo" / "config.toml").write_text("[push]\nverify = true\n")
    _add_prepush(work)
    res = land(work)
    assert not on_remote(origin, sha), "verify=true runs the failing hook, so nothing lands"
    assert res["ok"] is False and not res["landed"]


def test_land_is_not_reported_when_push_is_rejected(repo):
    """The other direction: nothing on the remote must never read as landed."""
    origin, work, sha = repo
    # A pre-receive that refuses everything: the push fails for real.
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/bash\nexit 1\n")
    hook.chmod(0o755)
    res = land(work)
    assert not on_remote(origin, sha), "precondition: the push really was refused"
    assert res["ok"] is False and not res["landed"]


def test_commit_landed_under_a_concurrent_push_is_not_called_a_failure(repo):
    """THE RACE. Our commit reaches main; another lands on top before we look.

    Equality against the branch TIP answers "am I the newest commit?", which is
    not the question. The question is "is my work on the branch?" — and it is.
    Reporting failure here is the verifier committing the exact sin it exists to
    catch, and it fires an urgent DM telling the requester to go fix a land that
    already succeeded.
    """
    origin, work, sha = repo
    race_on_receive(origin)
    res = land(work)
    assert on_remote(origin, sha), "precondition: our commit IS on origin/main"
    assert res["landed"] is True, (
        f"land() disowned a commit that is on the remote: {res.get('reason')}"
    )
    assert res["ok"] is True


def test_unverifiable_push_is_not_reported_as_not_landed(repo, monkeypatch):
    """'I could not check' is not 'I did not do it'.

    If ls-remote itself fails, the honest answer is that we do not know. Calling
    that a failed land is a claim the daemon cannot support — the same
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
    res = land(work)
    assert on_remote(origin, sha), "precondition: the push really did land"
    assert res["landed"] is not False, (
        "claimed the push did not land when it could not see the remote at all"
    )
    assert "verif" in (res.get("reason") or "").lower(), (
        f"an unverifiable land must say so, got: {res.get('reason')!r}"
    )


# --------------------------------------------------------------------------- #
# What counts as "dirty"
#
# Refusing a dirty tree is right, but only tracked changes can reach a commit and
# therefore the push. Blocking on an untracked scratch file makes a correct
# refusal look arbitrary — and the obvious way past an arbitrary gate is to start
# deleting files.
# --------------------------------------------------------------------------- #
def test_untracked_files_do_not_block_a_land(repo):
    origin, work, sha = repo
    (work / "scratch.md").write_text("notes to self\n")
    (work / "build.log").write_text("noise\n")

    res = land(work)
    assert res["ok"] and res["landed"] is True, (
        f"an untracked file blocked a land it cannot affect: {res.get('reason')}"
    )
    assert on_remote(origin, sha)
    assert (work / "scratch.md").exists(), "land must not clean the tree"
    status = next(s for s in res["steps"] if s["step"] == "status")
    assert "untracked" in status["out"], "the ignored files should be visible"


def test_modified_tracked_file_still_blocks(repo):
    _origin, work, _sha = repo
    (work / "f.txt").write_text("half-written\n")
    res = land(work)
    assert res["ok"] is False and not res.get("landed")
    assert "tracked" in res["reason"]
    assert "f.txt" in res["reason"]


def test_staged_but_uncommitted_change_still_blocks(repo):
    _origin, work, _sha = repo
    (work / "new.txt").write_text("staged\n")
    sh(["git", "add", "new.txt"], work)
    res = land(work)
    assert res["ok"] is False and "new.txt" in res["reason"]


def test_claude_own_settings_gets_a_pointed_hint(repo):
    """Claude Code rewrites .claude/settings.json when you accept a permission.
    An agent told only 'dirty' will not think to look there — it didn't do it.
    """
    _origin, work, _sha = repo
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
    sh(["git", "add", ".claude/settings.json"], work)
    sh(["git", "commit", "-m", "settings"], work)
    (work / ".claude" / "settings.json").write_text('{"permissions": {"allow": []}}\n')

    res = land(work)
    assert res["ok"] is False
    assert "Claude Code's own settings file" in res["reason"]

    # ...but not when real work is dirty too: then the hint would be a lie.
    (work / "f.txt").write_text("also mine\n")
    res = land(work)
    assert "Claude Code's own settings file" not in res["reason"]


def test_manual_push_mode_rejects_the_land(repo):
    """A repo on push.mode = 'manual' has opted out of cabildo's push path. The
    daemon must refuse a land that reaches it anyway (stale queue entry, or a
    hand-filed request), and refuse it before touching the tree or the remote.
    """
    origin, work, sha = repo
    cabildo_dir = work / ".cabildo"
    cabildo_dir.mkdir()
    (cabildo_dir / "config.toml").write_text('[push]\nmode = "manual"\n')

    with pytest.raises(actions.Reject) as excinfo:
        land(work)
    assert "manual" in str(excinfo.value)
    assert not on_remote(origin, sha), "nothing must have been pushed"
