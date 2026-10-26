"""Pushes to different projects run in parallel; pushes to one project do not."""

from __future__ import annotations

from cabildo import store

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
