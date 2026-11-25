"""siblings.py and the MCP tools: start a Claude in the host, type to it, list it."""

from __future__ import annotations

import os
import stat
import time

import pytest

from cabildo import host, host_client, siblings, store
from cabildo import mcp_server as srv

from .host_helpers import read_until, viewer

STAND_IN = """#!/bin/sh
printf 'ARGS %s\\n' "$*"
printf 'DEPTH %s CHILD [%s]\\n' "$CABILDO_SIBLING_DEPTH" "$CLAUDE_CODE_CHILD_SESSION"
exec cat
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch) -> str:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(STAND_IN)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(siblings, "CLAUDE", str(path))
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")  # set, as in a Claude's shell
    monkeypatch.delenv(siblings.DEPTH_VAR, raising=False)
    return str(path)


@pytest.fixture(autouse=True)
def _no_submit_wait(monkeypatch) -> None:
    monkeypatch.setattr(host, "_SUBMIT_DELAY", 0.0)


def test_input_types_into_the_session_and_presses_enter(session_host, tmp_path):
    record = host_client.start_session(["sh", "-c", "read x; echo got-$x"], cwd=str(tmp_path))
    sock = viewer(session_host.path, record.id)
    host_client.send_input(record.id, "hello")
    assert b"got-hello" in read_until(sock, b"got-hello")[0]


def test_input_without_submit_leaves_the_text_unsent(session_host, tmp_path):
    record = host_client.start_session(["cat", "-v"], cwd=str(tmp_path))
    sock = viewer(session_host.path, record.id)
    host_client.send_input(record.id, "draft", submit=False)
    seen, _ = read_until(sock, b"draft")
    time.sleep(0.2)
    assert b"draft" in seen


def test_several_lines_go_in_as_one_paste(session_host, tmp_path):
    record = host_client.start_session(["cat", "-v"], cwd=str(tmp_path))
    sock = viewer(session_host.path, record.id)
    host_client.send_input(record.id, "a\nb", submit=False)
    seen, _ = read_until(sock, b"b")
    assert b"200~a" in seen  # the paste markers, shown by `cat -v`


def test_input_to_an_unknown_session_is_refused(session_host):
    with pytest.raises(host_client.ClientError, match="no hosted session"):
        host_client.send_input("nope", "x")


def test_a_sibling_gets_the_brief_the_depth_and_a_clean_environment(
    session_host, fake_claude, tmp_path
):
    record = siblings.start(
        parent_id="P1", parent_handle="alice", prompt="count the tests", cwd=str(tmp_path)
    )
    assert record.parent == "P1"
    sock = viewer(session_host.path, record.id)
    seen, _ = read_until(sock, b"CHILD [")
    text = seen.decode()
    assert "started by alice (session P1)" in text and "count the tests" in text
    assert "DEPTH 1 CHILD []" in text  # one level down; the caller's marker did not come along


def test_a_sibling_without_a_prompt_starts_empty(session_host, fake_claude, tmp_path):
    record = siblings.start(parent_id="P1", cwd=str(tmp_path), permission_mode="plan")
    seen, _ = read_until(viewer(session_host.path, record.id), b"CHILD [")
    assert b"ARGS --permission-mode plan\r\n" in seen


def test_the_cap_stops_a_fifth_sibling(session_host, fake_claude, tmp_path):
    for _ in range(siblings.MAX_SIBLINGS):
        siblings.start(parent_id="P1", cwd=str(tmp_path))
    with pytest.raises(siblings.SiblingError, match="cap"):
        siblings.start(parent_id="P1", cwd=str(tmp_path))


def test_the_depth_limit_stops_a_third_level(session_host, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv(siblings.DEPTH_VAR, str(siblings.MAX_DEPTH))
    with pytest.raises(siblings.SiblingError, match="depth"):
        siblings.start(parent_id="P1", cwd=str(tmp_path))


def test_a_missing_folder_is_refused(session_host, fake_claude):
    with pytest.raises(siblings.SiblingError, match="no such directory"):
        siblings.start(parent_id="P1", cwd="/no/such/dir")


# --- the MCP tools ---------------------------------------------------------------------


@pytest.fixture
def agent(conn, monkeypatch):
    store.register_session(
        conn, "A", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=111
    )
    store.set_handle(conn, "A", "alice")
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: 111)


def test_the_tools_start_send_and_list(session_host, fake_claude, agent, tmp_path):
    started = srv.sibling_start(prompt="hello there", cwd=str(tmp_path))
    assert started["ok"] is True
    sock = viewer(session_host.path, started["id"])
    assert b"started by alice (session A)" in read_until(sock, b"hello there")[0]
    assert srv.sibling_send(started["id"], "next")["ok"] is True
    assert b"next" in read_until(sock, b"next")[0]
    (mine,) = srv.siblings()["sessions"]
    assert (mine["id"], mine["parent"], mine["mine"]) == (started["id"], "A", True)


def test_a_tool_reports_an_error_instead_of_raising(session_host, fake_claude, agent):
    assert srv.sibling_start(cwd="/no/such/dir")["ok"] is False
    assert srv.sibling_send("nope", "x")["ok"] is False
    assert os.environ.get(siblings.DEPTH_VAR) is None


def test_siblings_names_the_bus_session_of_each_one(
    session_host, fake_claude, agent, conn, tmp_path
):
    started = srv.sibling_start(cwd=str(tmp_path))
    record = host_client.list_sessions()[0]
    store.register_session(
        conn, "S9", cwd="/x", project="/x", project_name="x", claude_pid=record.pid
    )
    (mine,) = srv.siblings()["sessions"]
    assert (mine["id"], mine["session_id"]) == (started["id"], "S9")


def test_sibling_kill_ends_it_and_refuses_an_unknown_one(
    session_host, fake_claude, agent, tmp_path
):
    started = srv.sibling_start(cwd=str(tmp_path))
    assert srv.sibling_kill(started["id"])["ok"] is True
    deadline = time.monotonic() + 5
    while srv.siblings()["sessions"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.siblings()["sessions"] == []
    assert srv.sibling_kill("nope")["ok"] is False


def test_host_stop_refuses_while_siblings_run_and_stops_when_forced(
    session_host, fake_claude, agent, tmp_path
):
    srv.sibling_start(cwd=str(tmp_path))
    refused = srv.host_stop()
    assert refused["ok"] is False and "still run" in refused["error"]
    assert srv.host_stop(force=True) == {"ok": True, "ended": 1}
