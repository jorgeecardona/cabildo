"""The gate/provision scripts are handed the affected-file set so a repo can
scope its work to what a push changes instead of gating every project
(issue #17). cabildo exposes the facts; the scoping lives in the repo's gate.
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


def test_run_passes_env_through():
    rc, out = actions.run(
        ["sh", "-c", "echo $CABILDO_BASE_REF"], cwd=".", env={"CABILDO_BASE_REF": "origin/main"}
    )
    assert rc == 0
    assert out == "origin/main"


def test_affected_env_lists_only_this_lanes_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _sh(["git", "init", "-b", "main"], repo)
    (repo / "a.py").write_text("a\n")
    _sh(["git", "add", "."], repo)
    _sh(["git", "commit", "-m", "base"], repo)
    # A branch ahead of main that adds one file.
    _sh(["git", "checkout", "-b", "feature/x"], repo)
    (repo / "b.py").write_text("b\n")
    _sh(["git", "add", "."], repo)
    _sh(["git", "commit", "-m", "work"], repo)

    env = actions._affected_env(str(repo), "main")
    changed = env["CABILDO_CHANGED_FILES"].split()
    assert changed == ["b.py"]  # only the lane's change, not the base file
    assert env["CABILDO_BASE_REF"] == "main"
    assert len(env["CABILDO_HEAD"]) >= 7  # a resolved sha


def test_affected_env_empty_file_list_when_base_unknown(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _sh(["git", "init", "-b", "main"], repo)
    (repo / "a.py").write_text("a\n")
    _sh(["git", "add", "."], repo)
    _sh(["git", "commit", "-m", "base"], repo)
    # A base ref that does not exist: the diff fails, but the refs still come back
    # so a gate can fall back to its own detection rather than see "nothing changed".
    env = actions._affected_env(str(repo), "origin/nope")
    assert env["CABILDO_CHANGED_FILES"] == ""
    assert env["CABILDO_BASE_REF"] == "origin/nope"
