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


def test_set_role_inherits_roster_responsibilities(two_agents, monkeypatch):
    two_agents(111)
    monkeypatch.setattr(
        srv.config, "roles", lambda *a, **k: {"reviewer": "Review diffs before push"}
    )
    out = srv.set_role("reviewer")
    assert out["ok"] is True
    assert out["from_roster"] is True
    assert out["responsibilities"] == "Review diffs before push"
    assert srv.whoami()["role"] == "reviewer"
    assert srv.whoami()["responsibilities"] == "Review diffs before push"


def test_set_role_freehand_and_override(two_agents, monkeypatch):
    two_agents(111)
    monkeypatch.setattr(srv.config, "roles", lambda *a, **k: {"reviewer": "roster text"})
    # A freehand role not in the roster: no inherited text.
    out = srv.set_role("firefighter")
    assert out["from_roster"] is False
    assert out["responsibilities"] is None
    # An explicit responsibilities argument overrides the roster default.
    out = srv.set_role("reviewer", "my own scope")
    assert out["from_roster"] is True
    assert out["responsibilities"] == "my own scope"


def test_set_role_clear(two_agents):
    two_agents(111)
    srv.set_role("reviewer", "x")
    out = srv.set_role(None)
    assert out["role"] is None
    assert srv.whoami()["role"] is None
    assert srv.whoami()["responsibilities"] is None


def test_send_to_role_fans_out_to_holders_only(two_agents):
    # B holds 'reviewer'; A routes work to the role without naming B.
    two_agents(222)
    srv.set_role("reviewer")
    two_agents(111)
    out = srv.send_to_role("reviewer", "please review #123")
    assert out["delivered"] == 1
    assert out["recipients"][0]["session_id"] == "B"
    # B actually receives it as a DM.
    two_agents(222)
    box = srv.inbox()["messages"]
    assert any(m["body"] == "please review #123" for m in box)


def test_send_to_role_empty_when_no_holder(two_agents):
    two_agents(111)
    out = srv.send_to_role("nobody-has-this", "hello")
    assert out["delivered"] == 0
    assert out["recipients"] == []


def test_list_agents_surfaces_role(two_agents):
    # B declares a role; A sees it on the card.
    two_agents(222)
    srv.set_role("reviewer", "Review diffs before push")
    two_agents(111)
    card = {p["session_id"]: p for p in srv.list_agents()}["B"]
    assert card["role"] == "reviewer"
    assert card["responsibilities"] == "Review diffs before push"


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


# --------------------------------------------------------------------------- #
# read() accepts the natural id aliases and no longer silently no-ops (issue #23)
# --------------------------------------------------------------------------- #
def test_read_accepts_ids_alias(two_agents):
    two_agents(111)
    mid = srv.send("mark me", to="bob")["message_id"]
    two_agents(222)
    res = srv.read(ids=[mid])  # the wrong-but-obvious name used to no-op
    assert res["marked_read"] == 1
    assert res["unread_remaining"] == 0


def test_read_accepts_singular_id_alias(two_agents):
    two_agents(111)
    mid = srv.send("mark me too", to="bob")["message_id"]
    two_agents(222)
    assert srv.read(id=mid)["marked_read"] == 1


def test_read_zero_match_explains_itself(two_agents):
    two_agents(222)
    res = srv.read(ids=[999999])  # not in bob's inbox
    assert res["marked_read"] == 0
    assert "note" in res and "no match" in res["note"]


def test_read_nothing_specified_says_so(two_agents):
    two_agents(222)
    res = srv.read()
    assert res["marked_read"] == 0
    assert "note" in res and "nothing to do" in res["note"]


# --------------------------------------------------------------------------- #
# Cross-project consent: directory, request_access, grant/deny, gated topics
# (issue #26)
# --------------------------------------------------------------------------- #
@pytest.fixture
def two_projects(conn, monkeypatch):
    """Agent A on project 'crm' (pid 111), agent B on project 'paas' (pid 222),
    plus the human (pid 999). Returns a knob to pick who 'we' are.
    """
    store.register_session(
        conn, "A", cwd="/repo/crm", project="/repo/crm", project_name="crm", claude_pid=111
    )
    store.register_session(
        conn, "B", cwd="/repo/paas", project="/repo/paas", project_name="paas", claude_pid=222
    )
    store.register_session(conn, store.HUMAN_SESSION, cwd="/home", claude_pid=999)
    store.set_handle(conn, "A", "ava")
    store.set_handle(conn, "B", "ben")
    current = {"pid": 111}
    monkeypatch.setattr(srv, "resolve_claude_pid", lambda: current["pid"])
    return lambda pid: current.__setitem__("pid", pid)


def test_cross_project_dm_blocked_until_granted(two_projects):
    two_projects(111)  # crm
    res = srv.send("hi paas", to="ben")
    assert res["ok"] is False
    assert "not reachable" in res["error"]
    # crm requests send access to paas; the human grants it.
    req = srv.request_access("paas", capability="send", kind="project")
    assert req["ok"] is True
    two_projects(999)  # human may always decide
    assert srv.grant(req["grant_id"])["ok"] is True
    two_projects(111)
    assert srv.send("hi paas", to="ben")["ok"] is True


def test_project_approver_lets_an_agent_decide(two_projects, home):
    (home / "config.toml").write_text('[project.paas]\ngrants = { approver = "project" }\n')
    two_projects(111)  # crm asks
    req = srv.request_access("paas", capability="send", kind="project")
    two_projects(222)  # an agent in paas decides, because paas set approver=project
    assert srv.grant(req["grant_id"])["ok"] is True
    two_projects(111)
    assert srv.send("now allowed", to="ben")["ok"] is True


def test_agent_cannot_decide_when_approver_is_human(two_projects):
    two_projects(111)
    req = srv.request_access("paas", capability="send", kind="project")
    two_projects(222)  # paas agent, but default approver is human
    res = srv.grant(req["grant_id"])
    assert res["ok"] is False
    assert "not an approver" in res["error"]


def test_directory_lists_projects_without_delivery(two_projects):
    two_projects(111)
    names = {row["project"].lower() for row in srv.directory()}
    assert {"crm", "paas"} <= names


def test_gated_topic_requires_listen_grant(two_projects):
    # paas gates a room; crm cannot join without a listen grant.
    two_projects(222)
    srv.gate_topic("war-room", True)
    two_projects(111)
    blocked = srv.join_topic("war-room")
    assert blocked["ok"] is False
    assert "gated" in blocked["error"]
    # Grant crm a listen (human), then crm can join.
    req = srv.request_access("war-room", capability="listen", kind="topic")
    two_projects(999)
    assert srv.grant(req["grant_id"])["ok"] is True
    two_projects(111)
    assert srv.join_topic("war-room")["ok"] is True


def test_version_reports_the_server_and_the_daemon(two_agents):
    import json
    import sys

    from cabildo import __version__
    from cabildo.daemon import info_path

    assert srv.version() == {"version": __version__, "exe": sys.executable, "daemon": None}
    info_path().write_text(json.dumps({"pid": 7, "version": "0.0.1", "exe": "/x/python"}))
    assert srv.version()["daemon"] == {"version": "0.0.1", "pid": 7, "exe": "/x/python"}


# --------------------------------------------------------------------------- #
# Agents on a paired machine, in a project this machine does not have
# --------------------------------------------------------------------------- #
def _remote(conn, sid="R", handle="rita", project="gamma"):
    store.replace_remote_sessions(
        conn, "nodeB", [{"session_id": sid, "handle": handle, "project_name": project}]
    )


def test_list_agents_shows_a_remote_agent_of_an_unknown_project(conn, two_agents):
    _remote(conn)
    ids = {a["session_id"]: a for a in srv.list_agents()}
    assert "R" in ids and ids["R"]["node"] == "nodeB"[:8]
    assert "R" in {a["session_id"] for a in srv.list_agents(all_projects=True)}


def test_dm_to_a_remote_agent_of_another_project_is_allowed(conn, two_agents):
    _remote(conn)
    out = srv.send(body="hello", to="@rita")
    assert out["ok"] is True
    row = conn.execute("SELECT to_session FROM messages WHERE body='hello'").fetchone()
    assert row["to_session"] == "R"


def test_dm_to_a_local_agent_of_an_unlinked_project_is_still_refused(conn, two_agents):
    store.register_session(
        conn, "L", cwd="/repo/zeta", project="/repo/zeta", project_name="zeta", claude_pid=333
    )
    store.set_handle(conn, "L", "lena")
    assert srv.send(body="hi", to="@lena")["ok"] is False


def test_a_room_post_stamped_before_the_join_is_delivered_when_it_arrives_after(conn):
    store.register_session(conn, "S", cwd="/r/a", project="/r/a", project_name="a", claude_pid=9)
    store.subscribe(conn, "S", "den")
    skewed = store._now() - 30  # the sender's clock runs 30 s behind this one
    store.post_message(
        conn,
        from_session="x",
        from_handle=None,
        body="late clock",
        topic="den",
        created_at=skewed,
        provenance="lan",
    )
    assert [m["body"] for m in store.inbox(conn, "S")] == ["late clock"]
