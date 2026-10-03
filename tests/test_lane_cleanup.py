"""Land-lane cleanup: a verified-landed throwaway worktree is removed, but only
under strict guards — never a main checkout, a non-land branch, a dirty lane, or
when disabled (issue #24). Real git, no mocking: the claim is "the worktree is
gone from git's point of view".
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


def _repo_with_lane(tmp_path, branch="land/topic"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _sh(["git", "init", "-b", "main"], repo)
    (repo / "f").write_text("x\n")
    _sh(["git", "add", "."], repo)
    _sh(["git", "commit", "-m", "init"], repo)
    lane = tmp_path / "lane"
    _sh(["git", "worktree", "add", "-b", branch, str(lane)], repo)
    return repo, lane


def test_removes_a_clean_landed_lane(tmp_path):
    repo, lane = _repo_with_lane(tmp_path)
    assert lane.exists()
    res = actions._remove_landed_lane(
        str(lane), "land/topic", "landed", [], actions.Ctx(worker="t")
    )
    assert res["removed"] is True
    assert not lane.exists()
    assert "land/topic" not in _sh(["git", "worktree", "list", "--porcelain"], repo)


def test_off_keeps_the_lane(tmp_path):
    _, lane = _repo_with_lane(tmp_path)
    res = actions._remove_landed_lane(str(lane), "land/topic", "off", [], actions.Ctx(worker="t"))
    assert res["removed"] is False
    assert res["reason"] == "off"
    assert lane.exists()


def test_non_land_branch_is_left_alone(tmp_path):
    _, lane = _repo_with_lane(tmp_path, branch="feature/x")
    res = actions._remove_landed_lane(str(lane), "feature/x", "landed", [], actions.Ctx(worker="t"))
    assert res["removed"] is False
    assert res["reason"] == "not-a-lane"
    assert lane.exists()


def test_main_checkout_is_never_removed(tmp_path):
    repo, _ = _repo_with_lane(tmp_path)
    # Even with a land-looking branch name, a main checkout (.git is a dir) is refused.
    res = actions._remove_landed_lane(
        str(repo), "land/topic", "landed", [], actions.Ctx(worker="t")
    )
    assert res["removed"] is False
    assert res["reason"] == "not-a-lane"
    assert repo.exists()


def test_dirty_lane_is_kept(tmp_path):
    _, lane = _repo_with_lane(tmp_path)
    (lane / "f").write_text("uncommitted change\n")  # modify a TRACKED file
    res = actions._remove_landed_lane(
        str(lane), "land/topic", "landed", [], actions.Ctx(worker="t")
    )
    assert res["removed"] is False
    assert res["reason"] == "dirty"
    assert lane.exists()
