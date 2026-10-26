"""Pushes to different projects run in parallel; pushes to one project do not."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from cabildo import actions, daemon, store

from .conftest import make_session


def queue(conn, *, project: str | None, action: str = "push") -> int:
    make_session(conn, "s1", handle="worker")
    return store.request_intent(
        conn,
        from_session="s1",
        from_handle="worker",
        project=project,
        action=action,
        params={"cwd": "/nope", "target": "main"},
    )


def take(conn) -> int | None:
    intent = store.take_next_intent(conn, "w")
    return None if intent is None else intent["id"]


def test_pushes_to_different_projects_run_at_the_same_time(conn):
    a = queue(conn, project="alpha")
    b = queue(conn, project="beta")
    assert take(conn) == a
    assert take(conn) == b


def test_second_push_to_a_project_waits_for_the_first(conn):
    a1 = queue(conn, project="alpha")
    a2 = queue(conn, project="alpha")
    b = queue(conn, project="beta")
    assert take(conn) == a1
    # a2 is older than b, but its lane is busy: b goes first.
    assert take(conn) == b
    assert take(conn) is None
    store.finish_intent(conn, a1, status="done", result={})
    assert take(conn) == a2


def test_pushes_without_a_project_share_one_lane(conn):
    first = queue(conn, project=None)
    queue(conn, project=None)
    assert take(conn) == first
    assert take(conn) is None


def test_spawns_share_one_lane_across_projects(conn):
    # spawn checks one max_agents cap, so two spawns must not run together.
    s1 = queue(conn, project="alpha", action="spawn")
    queue(conn, project="beta", action="spawn")
    p = queue(conn, project="beta")
    assert take(conn) == s1
    assert take(conn) == p
    assert take(conn) is None


def git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_two_projects_in_one_repo_share_a_push_lane(conn, tmp_path):
    # Two pinned packages of one monorepo are two projects but one working tree.
    repo = git_repo(tmp_path / "mono")
    (repo / "pkg-a").mkdir()
    (repo / "pkg-b").mkdir()
    a = store.request_intent(
        conn,
        from_session="s1",
        from_handle=None,
        project="pkg-a",
        action="push",
        params={"cwd": str(repo / "pkg-a")},
    )
    store.request_intent(
        conn,
        from_session="s1",
        from_handle=None,
        project="pkg-b",
        action="push",
        params={"cwd": str(repo / "pkg-b")},
    )
    assert take(conn) == a
    assert take(conn) is None


def test_a_worktree_shares_the_push_lane_of_its_repo(tmp_path):
    repo = git_repo(tmp_path / "main")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
    assert store.intent_lane("push", "a", {"cwd": str(repo)}) == store.intent_lane(
        "push", "b", {"cwd": str(wt)}
    )


def test_a_daemon_exiting_fails_its_own_running_rows(conn):
    a = queue(conn, project="alpha")
    store.take_next_intent(conn, "me")
    queue(conn, project="beta")
    store.take_next_intent(conn, "other")
    assert store.reap_stale_intents(conn, 0, worker="me") == 1
    row = conn.execute("SELECT status FROM intents WHERE id=?", (a,)).fetchone()
    assert row["status"] == "failed"


def test_an_error_before_the_action_fails_the_row(conn, monkeypatch):
    a = queue(conn, project="alpha")
    intent = store.take_next_intent(conn, "w")
    assert intent is not None
    real = store.connect
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(**kw)

    monkeypatch.setattr(store, "connect", flaky)
    daemon._handle_on_thread(intent, actions.Ctx(worker="w", log=lambda _m: None))
    row = conn.execute("SELECT status FROM intents WHERE id=?", (a,)).fetchone()
    assert row["status"] == "failed"
