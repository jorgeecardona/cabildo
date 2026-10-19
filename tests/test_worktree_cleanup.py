"""Worktree cleanup: a worktree whose work is already merged into the remote
target is removed, but only under strict guards — never a main checkout, a
worktree with unpushed work, a dirty one, or when disabled (issue #24). Real
git, no mocking: the claim is "the worktree is gone from git's point of view".
"""

from __future__ import annotations

import os
import subprocess

from cabildo import actions

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def _sh(cmd: list[str], cwd) -> str:
    p = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **_ENV}
    )
    assert p.returncode == 0, f"{cmd} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def _repo_with_worktree(tmp_path, *, ahead: bool = False):
    """A repo with an `origin` remote and one linked worktree on `feature/x`.
    With ahead=True the worktree carries a commit that is NOT yet on origin/main.
    """
    origin = tmp_path / "origin.git"
    _sh(["git", "init", "-q", "--bare", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _sh(["git", "init", "-q", "-b", "main"], repo)
    _sh(["git", "remote", "add", "origin", str(origin)], repo)
    (repo / "a.py").write_text("a\n")
    _sh(["git", "add", "."], repo)
    _sh(["git", "commit", "-q", "-m", "base"], repo)
    _sh(["git", "push", "-q", "origin", "main"], repo)
    wt = tmp_path / "wt"
    _sh(["git", "worktree", "add", "-q", "-b", "feature/x", str(wt)], repo)
    if ahead:
        (wt / "b.py").write_text("b\n")
        _sh(["git", "add", "."], wt)
        _sh(["git", "commit", "-q", "-m", "unpushed"], wt)
    return repo, wt


def _call(wt, mode):
    return actions._remove_merged_worktree(
        str(wt), "feature/x", "origin", "main", mode, [], actions.Ctx(worker="t")
    )


def test_removes_a_merged_worktree(tmp_path):
    repo, wt = _repo_with_worktree(tmp_path)  # HEAD == origin/main, nothing to lose
    assert wt.exists()
    res = _call(wt, "merged")
    assert res["removed"] is True
    assert not wt.exists()
    assert "feature/x" not in _sh(["git", "worktree", "list", "--porcelain"], repo)


def test_off_keeps_the_worktree(tmp_path):
    _, wt = _repo_with_worktree(tmp_path)
    res = _call(wt, "off")
    assert res["removed"] is False and res["reason"] == "off"
    assert wt.exists()


def test_unmerged_worktree_is_kept(tmp_path):
    _, wt = _repo_with_worktree(tmp_path, ahead=True)  # has a commit not on origin/main
    res = _call(wt, "merged")
    assert res["removed"] is False and res["reason"] == "not-merged"
    assert wt.exists()


def test_main_checkout_is_never_removed(tmp_path):
    repo, _ = _repo_with_worktree(tmp_path)
    res = actions._remove_merged_worktree(
        str(repo), "main", "origin", "main", "merged", [], actions.Ctx(worker="t")
    )
    assert res["removed"] is False and res["reason"] == "not-a-worktree"
    assert repo.exists()


def test_dirty_worktree_is_kept(tmp_path):
    _, wt = _repo_with_worktree(tmp_path)
    (wt / "a.py").write_text("uncommitted change\n")  # modify a TRACKED file
    res = _call(wt, "merged")
    assert res["removed"] is False and res["reason"] == "dirty"
    assert wt.exists()
