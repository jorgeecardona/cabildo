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


# --------------------------------------------------------------------------- #
# Executable identification (the resolve_claude_pid matcher)
# --------------------------------------------------------------------------- #
from cabildo.context import _exe_id


def test_prompt_text_cannot_disqualify_claude():
    # The regression that misattributed messages: a -p prompt mentioning
    # 'cabildo' made the walk skip the agent's own claude process.
    argv = ["claude", "-p", "You are agent BOB on the cabildo bus."]
    exe = _exe_id(argv)
    assert "claude" in exe and "cabildo" not in exe


def test_versioned_claude_binary_matches():
    exe = _exe_id(["/home/u/.local/share/claude/versions/2.1.215", "--print"])
    assert "claude" in exe


def test_node_launched_claude_matches():
    exe = _exe_id(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
    assert "claude" in exe


def test_shell_wrapper_never_matches_even_quoting_claude():
    assert _exe_id(["/bin/bash", "-c", "source ~/.claude/x.sh && claude"]) == ""


def test_cabildo_binaries_are_excluded():
    for argv in (
        ["/home/u/.venv/bin/cabildo-mcp"],
        ["python3", "/home/u/.claude/venv/bin/cabildo-hook", "stop"],
    ):
        exe = _exe_id(argv)
        assert not ("claude" in exe and "cabildo" not in exe)
