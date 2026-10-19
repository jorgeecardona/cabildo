"""The branch/worktree inventory: real git against a real repo, plus attribution
pulled from the spawn ledger. Read-only — the tests assert it reports, and that it
never mutates the repo it inspects.
"""

from __future__ import annotations

import subprocess

import pytest

from cabildo import branches, store

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def sh(cmd, cwd, env=None):
    import os

    p = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **ENV, **(env or {})}
    )
    assert p.returncode == 0, f"{cmd} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A bare origin with `main` at base, a primary clone on main, and one linked
    worktree on a lane branch that is one commit ahead of main.
    """
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path)
    sh(["git", "clone", str(origin), str(work)], tmp_path)
    (work / "f.txt").write_text("base\n")
    sh(["git", "add", "f.txt"], work)
    sh(["git", "commit", "-m", "base"], work)
    sh(["git", "push", "origin", "main"], work)

    lane_wt = tmp_path / "lane_wt"
    sh(["git", "worktree", "add", "-b", "agent/qa/0928", str(lane_wt)], work)
    (lane_wt / "g.txt").write_text("lane\n")
    sh(["git", "add", "g.txt"], lane_wt)
    sh(["git", "commit", "-m", "lane work"], lane_wt)
    return origin, work, lane_wt


def test_inventory_lists_worktrees_and_places_them_against_trunk(repo, home):
    _, work, lane_wt = repo
    inv = branches.inventory(cwd=str(work))
    assert inv["ok"] is True
    paths = {w["path"]: w for w in inv["worktrees"]}
    # Both checkouts are present (paths are realpath-normalised by git).
    assert any(str(work) in p for p in paths)
    lane = next(w for w in inv["worktrees"] if w["branch"] == "agent/qa/0928")
    assert lane["ahead"] == 1 and lane["behind"] == 0
    assert lane["in_trunk"] is False  # its commit is not on origin/main yet
    main = next(w for w in inv["worktrees"] if w["branch"] == "main")
    assert main["ahead"] == 0 and main["behind"] == 0
    assert main["in_trunk"] is True


def test_dirty_count_ignores_untracked(repo, home):
    _, work, lane_wt = repo
    (lane_wt / "tracked.txt").write_text("x\n")
    sh(["git", "add", "tracked.txt"], lane_wt)  # staged tracked change
    (lane_wt / "scratch.log").write_text("noise\n")  # untracked: must not count
    inv = branches.inventory(cwd=str(work))
    lane = next(w for w in inv["worktrees"] if w["branch"] == "agent/qa/0928")
    assert lane["dirty"] == 1


def test_bare_branch_without_a_worktree_is_listed(repo, home):
    _, work, _ = repo
    sh(["git", "branch", "stale/idea"], work)  # a branch nothing is checked out on
    inv = branches.inventory(cwd=str(work))
    bare = {b["branch"] for b in inv["branches"]}
    assert "stale/idea" in bare
    assert "agent/qa/0928" not in bare  # it HAS a worktree, so not listed here


def test_worktree_is_attributed_to_its_spawn_ledger_agent(repo, home):
    _, work, lane_wt = repo
    conn = store.connect()
    store.register_session(conn, "parent", cwd=str(work))
    store.record_spawn(
        conn,
        tag="qa-0928",
        parent_session="parent",
        role="qa",
        task="audit the gate",
        worktree=str(lane_wt),
        branch="agent/qa/0928",
    )
    conn.close()
    inv = branches.inventory(cwd=str(work))
    lane = next(w for w in inv["worktrees"] if w["branch"] == "agent/qa/0928")
    assert lane["agent"] is not None
    assert lane["agent"]["status"] == "launched"


def test_inventory_does_not_mutate_the_repo(repo, home):
    _, work, _ = repo
    before = sh(["git", "-C", str(work), "rev-parse", "HEAD"], work)
    before_wts = sh(["git", "-C", str(work), "worktree", "list"], work)
    branches.inventory(cwd=str(work))
    assert sh(["git", "-C", str(work), "rev-parse", "HEAD"], work) == before
    assert sh(["git", "-C", str(work), "worktree", "list"], work) == before_wts
