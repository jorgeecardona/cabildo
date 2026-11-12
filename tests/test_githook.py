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


def test_pre_commit_allowed_in_a_shared_checkout_under_trunk_off(conn, tmp_path, monkeypatch):
    # #35: with trunk mode off (the default), committing your own paths on the
    # shared branch is the intended flow — the pre-commit hook must not block it.
    repo = _repo(tmp_path / "repo")
    _add_peer(conn, repo)
    monkeypatch.setenv("CABILDO_PROJECT_DIR", str(repo))
    assert cli.cmd_githook(_args("pre-commit")) == 0


def test_pre_commit_blocked_on_trunk_under_guard_mode(conn, tmp_path, home, monkeypatch):
    # Under the lane workflow a commit straight on the trunk is denied (by the
    # trunk guard), with or without a peer.
    repo = _repo(tmp_path / "repo")
    _add_peer(conn, repo)
    _, name = git_project(str(repo))
    (home / "config.toml").write_text(f'[project.{name}]\ntrunk = {{ mode = "guard" }}\n')
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


# --------------------------------------------------------------------------- #
# cabildo guard install / uninstall
# --------------------------------------------------------------------------- #
def _install_args(dry=False):
    return types.SimpleNamespace(print=dry)


def test_guard_install_never_writes_files(tmp_path, monkeypatch):
    # guard install is now read-only: hook files belong to `make install-hooks`.
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    assert cli.cmd_guard_install(_install_args()) == 0
    for event in ("pre-commit", "pre-push"):
        assert not (repo / ".git" / "hooks" / event).exists()


def test_guard_install_print_flag_still_writes_nothing(tmp_path, monkeypatch):
    # --print was the old "dry run"; now every call is dry.
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    assert cli.cmd_guard_install(_install_args(dry=True)) == 0
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


def test_guard_install_prints_make_install_hooks_guidance(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    cli.cmd_guard_install(_install_args())
    out = capsys.readouterr().out
    assert "make install-hooks" in out


def test_guard_install_from_subdirectory_resolves_hooks_dir(tmp_path, monkeypatch):
    # Regression: relative core.hooksPath was resolved against cwd (a subdirectory)
    # instead of the repo root, so the resolved path was wrong.
    repo = _repo(tmp_path / "repo")
    # Set a relative hooksPath, as `make install-hooks` does.
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".githooks"], check=True)
    subdir = repo / "apps" / "backend"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    # Should resolve to <repo>/.githooks, not <subdir>/.githooks.
    hooks_dir = cli._git_hooks_dir(str(subdir))
    assert hooks_dir is not None
    assert hooks_dir == repo / ".githooks"


def test_guard_uninstall_removes_only_our_block(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    # Write a hook that already has the cabildo block (as old installs did).
    block = (
        "# >>> cabildo guard >>>\ncabildo githook pre-commit || exit $?\n# <<< cabildo guard <<<\n"
    )
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env bash\n" + block + "echo existing\n")
    hook.chmod(hook.stat().st_mode | 0o111)
    cli.cmd_guard_uninstall(types.SimpleNamespace())
    text = hook.read_text()
    assert "cabildo githook" not in text
    assert "echo existing" in text  # the rest survives


def test_guard_install_rejects_a_non_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not a git work tree
    assert cli.cmd_guard_install(_install_args()) == 1
