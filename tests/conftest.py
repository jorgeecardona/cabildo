"""Test fixtures: every test gets its own throwaway DB via CABILDO_HOME."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cabildo import store


@pytest.fixture(autouse=True)
def _isolate_git(tmp_path, monkeypatch):
    """Keep tests that run git away from the real repo, even under a git hook."""
    for var in [v for v in os.environ if v.startswith("GIT_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    # No test may reach a network remote: git refuses ssh/https/git, local paths still work (#19-10).
    for i, proto in enumerate(("ssh", "https", "http", "git")):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"protocol.{proto}.allow")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", "never")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "4")


_REPO = Path(__file__).resolve().parent.parent


def _pollution() -> list[str]:
    """Return traces a test leaves in the real checkout; other agents' commits are not traces."""

    def git(*args: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(
            ["git", "-C", str(_REPO), *args], capture_output=True, text=True, env=env, check=False
        ).stdout

    found = [line for line in git("worktree", "list").splitlines() if "pytest-of-" in line]
    if git("config", "--local", "user.email").strip():
        found.append("local user.email is set")
    if git("log", "-1", "--format=%ae").strip() == "t@t":
        found.append("HEAD authored by t@t")
    return found


@pytest.fixture(autouse=True, scope="session")
def _real_repo_untouched():
    """Fail the run if a test left worktrees, git identity or commits in the real repo (#19-10)."""
    before = set(_pollution())
    yield
    new = set(_pollution()) - before
    assert not new, f"test run polluted the real repo: {sorted(new)}"


@pytest.fixture(autouse=True)
def _no_real_netrc(tmp_path, monkeypatch):
    """Keep the user's ``~/.netrc`` token out of tests that check where a token comes from."""
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def conn(home):
    c = store.connect()
    yield c
    c.close()


def make_session(
    conn, session_id, *, handle=None, project="/repo/alpha", project_name="alpha", claude_pid=1000
):
    """Register a session and optionally give it a handle."""
    store.register_session(
        conn,
        session_id,
        cwd=project,
        project=project,
        project_name=project_name,
        claude_pid=claude_pid,
    )
    if handle:
        store.set_handle(conn, session_id, handle)
    return session_id
