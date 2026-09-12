"""Test fixtures: every test gets its own throwaway DB via CABILDO_HOME."""

from __future__ import annotations

import pytest

from cabildo import store


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
