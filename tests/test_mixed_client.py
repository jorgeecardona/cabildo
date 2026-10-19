"""Mixed Claude/Codex operation on one local bus (#9).

Drives the real MCP tool surface as two independently identified sessions — a
Claude session (PID rendezvous) and a Codex session (CABILDO_CLIENT=codex +
owner-pid path from #6) — on a single temp database, and shows them discovering
each other and exchanging channel messages and DMs in both directions with
explicit read receipts. No Codex install required: identity is driven through
env + the patched process lookups, exactly as the live clients would set it.
"""

from __future__ import annotations

import pytest

from cabildo import mcp_server as srv
from cabildo import store


@pytest.fixture
def bus(conn, monkeypatch):
    """Two registered peers on one project, plus a `be(who)` switch selecting
    which one the next tool call resolves as.
    """
    store.register_session(conn, "claude-A", claude_pid=111, project="/repo/x", project_name="x")
    store.register_session(
        conn, "codex-B", client="codex", owner_pid=222, project="/repo/x", project_name="x"
    )
    store.set_handle(conn, "claude-A", "alice")
    store.set_handle(conn, "codex-B", "bob")

    state = {"claude_pid": 0, "ppid": 0}
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: state["claude_pid"])
    monkeypatch.setattr(srv.os, "getppid", lambda: state["ppid"])

    def be(who: str):
        for var in ("CABILDO_TAG", "CABILDO_CLIENT", "CABILDO_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)
        if who == "claude":
            state["claude_pid"], state["ppid"] = 111, 999
        elif who == "codex":
            monkeypatch.setenv("CABILDO_CLIENT", "codex")
            state["claude_pid"], state["ppid"] = 0, 222
        else:  # pragma: no cover
            raise ValueError(who)

    return be


def test_each_client_resolves_to_its_own_identity(bus):
    bus("claude")
    me = srv.whoami()
    assert me["session_id"] == "claude-A" and me["client"] == "claude"
    bus("codex")
    me = srv.whoami()
    assert me["session_id"] == "codex-B" and me["client"] == "codex"


def test_peers_discover_each_other_with_client_tag(bus):
    bus("codex")
    peers = {a["session_id"]: a for a in srv.list_agents()}
    assert peers["claude-A"]["client"] == "claude"
    bus("claude")
    peers = {a["session_id"]: a for a in srv.list_agents()}
    assert peers["codex-B"]["client"] == "codex"


def test_channel_message_claude_to_codex_with_read_receipt(bus):
    bus("claude")
    srv.send("standup in 5")  # defaults to the shared project channel
    bus("codex")
    box = srv.inbox(unread_only=True)["messages"]
    assert len(box) == 1 and box[0]["from"] == "@alice"
    assert srv.whoami()["unread"] == 1
    r = srv.read(message_ids=[box[0]["id"]])
    assert r["marked_read"] == 1 and r["unread_remaining"] == 0


def test_dm_both_directions(bus):
    # codex -> claude
    bus("codex")
    assert srv.send("can you take the parser?", to="alice")["ok"] is True
    bus("claude")
    box = srv.inbox(unread_only=True)["messages"]
    assert len(box) == 1 and box[0]["from"] == "@bob"
    srv.read(message_ids=[box[0]["id"]])

    # claude -> codex
    assert srv.send("yes, on it", to="bob")["ok"] is True
    bus("codex")
    box = srv.inbox(unread_only=True)["messages"]
    assert len(box) == 1 and box[0]["from"] == "@alice"
    assert srv.read(message_ids=[box[0]["id"]])["unread_remaining"] == 0


def test_sender_does_not_receive_own_channel_message(bus):
    bus("codex")
    srv.send("anyone around?")
    assert srv.whoami()["unread"] == 0  # own message is not unread to the sender
