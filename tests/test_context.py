"""Project detection: a cwd inside a git repo maps to the repo root."""

from __future__ import annotations

from cabildo.context import git_project


def test_git_project_finds_repo_root(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)

    root, name = git_project(str(sub))
    assert root == str(repo)
    assert name == "myrepo"


def test_git_project_falls_back_to_cwd_without_repo(tmp_path):
    plain = tmp_path / "loose"
    plain.mkdir()
    root, name = git_project(str(plain))
    assert root == str(plain)
    assert name == "loose"
