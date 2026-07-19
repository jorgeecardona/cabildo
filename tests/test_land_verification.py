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
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
}


def sh(cmd, cwd, env=None):
    import os
    p = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **ENV_ID, **(env or {})},
    )
    assert p.returncode == 0, f"{cmd} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def on_remote(origin, sha, branch="main") -> bool:
    """Ground truth, asked of the remote itself: is sha on the branch at all —
    tip or not? This is the question land() is really answering."""
    # --git-dir, not cwd: environments that set safe.bareRepository=explicit
    # (Claude Code injects it via GIT_CONFIG_*) refuse git run from inside a
    # bare repo unless the git dir is named explicitly.
    return subprocess.run(
        ["git", "--git-dir", str(origin), "merge-base", "--is-ancestor",
         sha, f"refs/heads/{branch}"],
        capture_output=True,
    ).returncode == 0


@pytest.fixture()
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
    agent (or Jorge's VS Code) landing between our push and our verify."""
    hook = origin / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/bash\n"
        "export GIT_DIR=$(pwd)\n"
        "tip=$(git rev-parse main); tree=$(git rev-parse main^{tree})\n"
        "c=$(GIT_AUTHOR_NAME=o GIT_AUTHOR_EMAIL=o@x GIT_COMMITTER_NAME=o "
        "GIT_COMMITTER_EMAIL=o@x git commit-tree \"$tree\" -p \"$tip\" -m other)\n"
        "git update-ref refs/heads/main \"$c\"\n"
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
