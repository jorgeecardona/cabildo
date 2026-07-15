"""Exercise the MCP tool surface agents actually call. Identity normally comes
from the PID rendezvous; here we drive it by patching resolve_claude_pid and
pre-registering sessions bound to those PIDs, simulating two agents."""

from __future__ import annotations

import pytest

from agent_chat import mcp_server as srv
from agent_chat import store


@pytest.fixture()
def two_agents(conn, monkeypatch):
    """Register agents A (pid 111) and B (pid 222) on the same project, and give
    a knob to switch which one 'we' are for the next tool call."""
    store.register_session(conn, "A", cwd="/repo/alpha", project="/repo/alpha",
                           project_name="alpha", claude_pid=111)
    store.register_session(conn, "B", cwd="/repo/alpha", project="/repo/alpha",
                           project_name="alpha", claude_pid=222)
    store.set_handle(conn, "A", "alice")
    store.set_handle(conn, "B", "bob")
    current = {"pid": 111}
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: current["pid"])

    def be(pid):
        current["pid"] = pid

    return be


def test_whoami_resolves_via_pid(two_agents):
    two_agents(111)
    me = srv.whoami()
    assert me["session_id"] == "A"
    assert me["handle"] == "alice"
    assert me["project"] == "alpha"


def test_send_to_handle_and_inbox_and_read_flow(two_agents):
    # alice DMs bob
    two_agents(111)
    res = srv.send("can you take the frontend?", to="bob")
    assert res["ok"] is True

    # bob sees it unread, reads inbox (no auto-mark), then explicitly reads it
    two_agents(222)
    box = srv.inbox()
    assert len(box) == 1
    assert box[0]["from"] == "@alice"
    assert box[0]["read"] is False
    mid = box[0]["id"]

    assert srv.whoami()["unread"] == 1
    r = srv.read(message_ids=[mid])
    assert r["marked_read"] == 1 and r["unread_remaining"] == 0
    assert srv.inbox(unread_only=True) == []


def test_send_defaults_to_project_channel(two_agents):
    two_agents(111)
    res = srv.send("team, standup in 5")
    assert res["to"] == f"#{store.project_topic('alpha')}"
    # bob (same project) receives it
    two_agents(222)
    assert srv.whoami()["unread"] == 1


def test_send_to_unknown_agent_fails(two_agents):
    two_agents(111)
    res = srv.send("hi", to="nobody")
    assert res["ok"] is False


def test_claim_release_via_tools(two_agents):
    two_agents(111)
    assert srv.claim("push:alpha", note="pushing")["ok"] is True
    # bob is blocked and told who holds it
    two_agents(222)
    blocked = srv.claim("push:alpha")
    assert blocked["ok"] is False and blocked["held_by"] == "@alice"
    assert srv.claims()[0]["resource"] == "push:alpha"
    # alice releases; bob can now take it
    two_agents(111)
    assert srv.release("push:alpha")["ok"] is True
    two_agents(222)
    assert srv.claim("push:alpha")["ok"] is True


def test_list_agents_scoped_to_project(two_agents):
    two_agents(111)
    peers = srv.list_agents()
    assert {p["session_id"] for p in peers} == {"B"}
