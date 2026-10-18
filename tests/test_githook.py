"""The opt-in git-hook guard: `cabildo githook <event>` runs the same
deny_git checks a plain terminal `git` would otherwise slip past, resolving a
human actor when there is no cabildo session, and failing open on any error.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from cabildo import cli, guard, store
from cabildo.context import git_project, tree_root


def _repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True
    )
    return path


def _args(event):
    return types.SimpleNamespace(event=event)


@pytest.fixture(autouse=True)
def _clean_identity(monkeypatch):
    # No inherited session identity, and no claude ancestor: resolve_actor falls
    # to the human default, deterministically, regardless of what ran the tests.
    for var in (
        "CABILDO_TAG",
        "CABILDO_SESSION_ID",
        "CABILDO_PROJECT_DIR",
        "CABILDO_GITHOOK_BYPASS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(guard, "resolve_claude_pid", lambda *a, **k: 0)


def _add_peer(conn, repo, sid="peer", handle="bob"):
    root, name = git_project(str(repo))
    store.register_session(conn, sid, cwd=str(repo), project=root, project_name=name, claude_pid=2)
    store.set_handle(conn, sid, handle)
    conn.commit()
    return root, name


# --------------------------------------------------------------------------- #
# resolve_actor
# --------------------------------------------------------------------------- #
def test_resolve_actor_defaults_to_the_human(conn, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("CABILDO_PROJECT_DIR", str(repo))
    me = guard.resolve_actor(conn)
    assert me.session_id == store.HUMAN_SESSION  # never None, never a real session
    assert me.handle == store.HUMAN_HANDLE
    root, name = git_project(str(repo))
    assert me.project == root and me.project_name == name
    assert me.cwd == tree_root(str(repo))


def test_resolve_actor_binds_an_explicit_session(conn, monkeypatch):
    store.register_session(
        conn, "sess1", cwd="/r/x", project="/r/x", project_name="x", claude_pid=1
    )
    monkeypatch.setenv("CABILDO_SESSION_ID", "sess1")
    assert guard.resolve_actor(conn).session_id == "sess1"


# --------------------------------------------------------------------------- #
# cmd_githook
# --------------------------------------------------------------------------- #
def test_pre_push_blocked_when_a_peer_is_live(conn, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    _add_peer(conn, repo)
    monkeypatch.setenv("CABILDO_PROJECT_DIR", str(repo))
    assert cli.cmd_githook(_args("pre-push")) == 1
    assert "@bob" in capsys.readouterr().err  # the reason reaches git's stderr


def test_pre_commit_blocked_in_a_shared_checkout(conn, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _add_peer(conn, repo)
    monkeypatch.setenv("CABILDO_PROJECT_DIR", str(repo))
    assert cli.cmd_githook(_args("pre-commit")) == 1


def test_githook_allows_a_lone_human(conn, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")  # no peer registered
    monkeypatch.setenv("CABILDO_PROJECT_DIR", str(repo))
    assert cli.cmd_githook(_args("pre-push")) == 0
    assert cli.cmd_githook(_args("pre-commit")) == 0


def test_githook_bypass_env_never_self_denies(conn, tmp_path, monkeypatch):
    # The daemon's own serialized push sets CABILDO_GITHOOK_BYPASS; even with a
    # live peer (which would otherwise deny), the hook steps aside.
    repo = _repo(tmp_path / "repo")
    _add_peer(conn, repo)
    monkeypatch.setenv("CABILDO_PROJECT_DIR", str(repo))
    monkeypatch.setenv("CABILDO_GITHOOK_BYPASS", "1")
    assert cli.cmd_githook(_args("pre-push")) == 0


def test_githook_fails_open_on_error(conn, tmp_path, monkeypatch):
    # A guard that raises must never block the user's git: exit 0, not a crash.
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("CABILDO_PROJECT_DIR", str(repo))
    monkeypatch.setattr(
        guard, "resolve_actor", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert cli.cmd_githook(_args("pre-push")) == 0
