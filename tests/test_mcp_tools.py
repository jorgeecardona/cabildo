"""Exercise the MCP tool surface agents actually call. Identity normally comes
from the PID rendezvous; here we drive it by patching resolve_claude_pid and
pre-registering sessions bound to those PIDs, simulating two agents.
"""

from __future__ import annotations

import pytest

from cabildo import mcp_server as srv
from cabildo import store


@pytest.fixture
def two_agents(conn, monkeypatch):
    """Register agents A (pid 111) and B (pid 222) on the same project, and give
    a knob to switch which one 'we' are for the next tool call.
    """
    store.register_session(
        conn, "A", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=111
    )
    store.register_session(
        conn, "B", cwd="/repo/alpha", project="/repo/alpha", project_name="alpha", claude_pid=222
    )
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
    box = srv.inbox()["messages"]
    assert len(box) == 1
    assert box[0]["from"] == "@alice"
    assert "read" not in box[0]  # unread_only: saying so would be noise
    mid = box[0]["id"]

    assert srv.whoami()["unread"] == 1
    r = srv.read(message_ids=[mid])
    assert r["marked_read"] == 1 and r["unread_remaining"] == 0
    assert srv.inbox(unread_only=True)["messages"] == []


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


def test_inbox_previews_long_bodies_and_get_message_fetches_full(two_agents):
    two_agents(111)
    long_body = "A" * 2000
    srv.send(long_body, to="bob")

    two_agents(222)
    box = srv.inbox()["messages"]
    assert len(box) == 1
    item = box[0]
    # The inbox item is a preview, not the whole 2000-char body.
    assert item["truncated"] is True
    assert len(item["body"]) <= srv.PREVIEW_CHARS + 5  # + " …"
    assert item["body"].endswith("…")

    # Fetch the full body by id; it does NOT mark the message read.
    full = srv.get_message(item["id"])
    assert full is not None
    assert full["body"] == long_body
    assert srv.whoami()["unread"] == 1


def test_inbox_short_body_is_not_truncated(two_agents):
    two_agents(111)
    srv.send("short one", to="bob")
    two_agents(222)
    item = srv.inbox()["messages"][0]
    assert item["body"] == "short one"
    assert "truncated" not in item


def test_inbox_full_flag_returns_complete_bodies(two_agents):
    two_agents(111)
    srv.send("B" * 1000, to="bob")
    two_agents(222)
    item = srv.inbox(full=True)["messages"][0]
    assert len(item["body"]) == 1000
    assert "truncated" not in item


def test_get_message_denies_out_of_inbox(two_agents):
    # alice sends bob a DM; alice must not fetch her own message back.
    two_agents(111)
    srv.send("for bob only", to="bob")
    two_agents(222)
    mid = srv.inbox()["messages"][0]["id"]
    two_agents(111)
    assert srv.get_message(mid) is None


# --------------------------------------------------------------------------- #
# Inbox budget
#
# Measured over two weeks of real use, `inbox` was 39% of everything cabildo
# costs in tokens, and the mean hid the shape: p50 1.7k chars, p90 4.8k, worst
# 43.6k in a single call. A per-message preview cap can't bound that — the cost
# scales with how many messages are waiting, so an agent returning to a busy
# channel pays the whole backlog at once.
# --------------------------------------------------------------------------- #
def _chars(payload) -> int:
    import json

    return len(json.dumps(payload))


def test_a_backlog_cannot_flood_the_context(two_agents):
    two_agents(111)
    for i in range(15):
        srv.send(f"status report {i}: " + "A" * 2000, to="bob")

    two_agents(222)
    res = srv.inbox()
    assert _chars(res) < srv.INBOX_BUDGET_CHARS * 1.6, (
        f"one inbox call returned {_chars(res)} chars"
    )
    assert len(res["messages"]) == 15
    assert all(m.get("truncated") for m in res["messages"])


def test_previews_shrink_as_the_backlog_grows(two_agents):
    two_agents(111)
    srv.send("X" * 3000, to="bob")
    two_agents(222)
    alone = len(srv.inbox()["messages"][0]["body"])

    two_agents(111)
    for _ in range(9):
        srv.send("X" * 3000, to="bob")
    two_agents(222)
    crowded = len(srv.inbox()["messages"][0]["body"])

    assert alone == srv.PREVIEW_CHARS + 2  # + " …"
    assert crowded < alone
    assert crowded >= srv.MIN_PREVIEW_CHARS  # still worth reading


def test_short_messages_hand_their_share_back(two_agents):
    """A budget split evenly would starve one long message among many short
    ones for no reason — the short ones never used their share.
    """
    two_agents(111)
    for _ in range(9):
        srv.send("ok", to="bob")
    srv.send("Y" * 3000, to="bob")

    two_agents(222)
    msgs = srv.inbox()["messages"]
    longest = max(msgs, key=lambda m: len(m["body"]))
    assert len(longest["body"]) > srv.INBOX_BUDGET_CHARS // 10


def test_more_counts_what_did_not_fit(two_agents):
    two_agents(111)
    for i in range(20):
        srv.send(f"msg {i}", to="bob")

    two_agents(222)
    res = srv.inbox(limit=5)
    assert len(res["messages"]) == 5
    assert res["more"] == 15, "an agent must be able to tell there's a backlog"
    assert srv.inbox()["more"] == 20 - srv.DEFAULT_INBOX_LIMIT


def test_more_is_zero_when_the_inbox_fits(two_agents):
    two_agents(111)
    srv.send("only one", to="bob")
    two_agents(222)
    assert srv.inbox()["more"] == 0


def test_full_still_means_full(two_agents):
    """The escape hatch has to actually escape — otherwise an agent that needs
    the whole thing has no way to get it except get_message in a loop.
    """
    two_agents(111)
    srv.send("Z" * 5000, to="bob")
    two_agents(222)
    item = srv.inbox(full=True)["messages"][0]
    assert len(item["body"]) == 5000
