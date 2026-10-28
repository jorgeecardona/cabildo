"""Push-delivery feed (GitLab #30): the id-cursor inbox query and the JSON CLI
verbs the bundled mod polls — `pull`, `send`, `mark-read`, `whoami`.

The mod runs outside a live agent turn, so it cannot use the MCP tools; these
verbs are its machine-readable surface, and they must reuse the store's own
inbox predicate rather than re-deriving "addressed to me".
"""

from __future__ import annotations

import json

from cabildo import cli, store

from .conftest import make_session


# --------------------------------------------------------------------------- #
# store.inbox_after / max_message_id
# --------------------------------------------------------------------------- #
def test_inbox_after_is_a_gap_free_id_cursor(conn):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    m1 = store.post_message(conn, from_session="A", from_handle="alice", body="one", to_session="B")
    m2 = store.post_message(conn, from_session="A", from_handle="alice", body="two", to_session="B")

    rows = store.inbox_after(conn, "B", after_id=0)
    assert [r["id"] for r in rows] == [m1, m2], "oldest first, by id"

    assert [r["id"] for r in store.inbox_after(conn, "B", after_id=m1)] == [m2]
    assert store.inbox_after(conn, "B", after_id=m2) == []


def test_inbox_after_never_returns_my_own_messages(conn):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    store.post_message(conn, from_session="A", from_handle="alice", body="to B", to_session="B")
    store.post_message(conn, from_session="B", from_handle="bob", body="to A", to_session="A")
    # B's feed has A's message, not B's own.
    rows = store.inbox_after(conn, "B", after_id=0)
    assert [r["body"] for r in rows] == ["to B"]


def test_inbox_after_urgent_only_and_read_state_rides_but_does_not_filter(conn):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    plain = store.post_message(
        conn, from_session="A", from_handle="alice", body="plain", to_session="B"
    )
    urgent = store.post_message(
        conn, from_session="A", from_handle="alice", body="urgent", to_session="B", urgent=True
    )
    assert [r["id"] for r in store.inbox_after(conn, "B", urgent_only=True)] == [urgent]

    # Marking read does not remove it from the cursor feed: the cursor is the
    # poller's own pointer, independent of read-state.
    store.mark_read(conn, "B", message_ids=[plain])
    rows = {r["id"]: r for r in store.inbox_after(conn, "B", after_id=0)}
    assert rows[plain]["read"] is True
    assert rows[urgent]["read"] is False
    assert set(rows) == {plain, urgent}


def test_max_message_id(conn):
    assert store.max_message_id(conn) == 0
    make_session(conn, "A")
    make_session(conn, "B")
    mid = store.post_message(conn, from_session="A", from_handle=None, body="hi", to_session="B")
    assert store.max_message_id(conn) == mid


# --------------------------------------------------------------------------- #
# CLI verbs (JSON out, ids exposed)
# --------------------------------------------------------------------------- #
def test_cli_pull_json_exposes_ids_and_cursor(conn, capsys):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    store.post_message(conn, from_session="A", from_handle="alice", body="hello B", to_session="B")

    assert cli.main(["pull", "--session", "B", "--after", "0", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "B"
    assert out["cursor"] == 1
    assert [m["id"] for m in out["messages"]] == [1]
    assert out["messages"][0]["from_session"] == "A"
    assert out["messages"][0]["from_display"] == "@alice"
    assert out["messages"][0]["preview"] == "hello B"


def test_cli_pull_previews_long_bodies(conn, capsys):
    make_session(conn, "A")
    make_session(conn, "B")
    store.post_message(conn, from_session="A", from_handle=None, body="x" * 500, to_session="B")
    cli.main(["pull", "--session", "B", "--preview-chars", "10", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["messages"][0]["preview"] == "x" * 10
    assert out["messages"][0]["truncated"] is True


def test_cli_send_posts_as_the_session_not_the_human(conn, capsys):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    assert cli.main(["send", "--session", "A", "--to", "bob", "--json", "--", "ping"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["from"] == "A"
    assert out["to"] == "B"
    # The message really landed in B's inbox, attributed to A (not @jorge).
    rows = store.inbox_after(conn, "B", after_id=0)
    assert rows[0]["from_session"] == "A"
    assert rows[0]["body"] == "ping"


def test_cli_send_needs_exactly_one_destination(conn):
    make_session(conn, "A")
    assert cli.main(["send", "--session", "A", "--json", "--", "no dest"]) == 1


def test_cli_mark_read(conn, capsys):
    make_session(conn, "A")
    make_session(conn, "B")
    mid = store.post_message(conn, from_session="A", from_handle=None, body="hi", to_session="B")
    assert cli.main(["mark-read", "--session", "B", "--ids", str(mid), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"marked": 1}
    # Idempotent: already read, nothing newly marked.
    cli.main(["mark-read", "--session", "B", "--ids", str(mid), "--json"])
    assert json.loads(capsys.readouterr().out) == {"marked": 0}


def test_cli_meter_records_a_hook_metric(conn, capsys):
    make_session(conn, "B")
    assert cli.main(["meter", "--session", "B", "--event", "push_wake", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    row = conn.execute("SELECT event, session_id FROM hook_metrics").fetchone()
    assert row["event"] == "push_wake"
    assert row["session_id"] == "B"


def test_cli_pull_exposes_spoof_resistant_identity_fields(conn, capsys):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B")
    store.post_message(conn, from_session="A", from_handle="alice", body="hi", to_session="B")
    cli.main(["pull", "--session", "B", "--json"])
    m = json.loads(capsys.readouterr().out)["messages"][0]
    assert m["from_robot"] and isinstance(m["from_robot"], str)
    assert m["from_human"] is False


def test_cli_send_enforces_cross_project_consent(conn, capsys):
    # A on alpha, B on beta, no grant/link: the session-identity CLI send must
    # refuse exactly as the MCP tool does (issue #26) — not a back door.
    make_session(conn, "A", handle="alice", project="/repo/alpha", project_name="alpha")
    make_session(conn, "B", handle="bob", project="/repo/beta", project_name="beta")
    store.unsubscribe(conn, "B", "general")  # no room in common
    conn.commit()
    assert cli.main(["send", "--session", "A", "--to", "bob", "--json", "--", "hi"]) == 1
    assert "not reachable" in capsys.readouterr().err
    assert store.inbox_after(conn, "B", after_id=0) == []


def test_cli_send_same_project_is_allowed(conn):
    make_session(conn, "A", handle="alice", project_name="alpha")
    make_session(conn, "B", handle="bob", project_name="alpha")
    assert cli.main(["send", "--session", "A", "--to", "bob", "--json", "--", "hi"]) == 0


def test_cli_unknown_session_is_refused(conn, capsys):
    make_session(conn, "A")
    assert cli.main(["pull", "--session", "ghost", "--json"]) == 1
    assert "no such session" in capsys.readouterr().err
    assert cli.main(["send", "--session", "ghost", "--to", "A", "--json", "--", "x"]) == 1


def test_cli_mark_read_rejects_non_integer_ids(conn, capsys):
    make_session(conn, "B")
    assert cli.main(["mark-read", "--session", "B", "--ids", "1,abc", "--json"]) == 1
    assert "integers" in capsys.readouterr().err


def test_cli_whoami_resolves_via_session_env(conn, capsys, monkeypatch):
    make_session(conn, "sess-x", handle="xavier")
    monkeypatch.setenv("CABILDO_SESSION_ID", "sess-x")
    assert cli.main(["whoami", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "sess-x"
    assert out["handle"] == "xavier"
    assert out["is_human"] is False
    assert out["cursor"] == store.max_message_id(conn)


def test_cli_send_reaches_a_paired_machines_agent_of_another_project(conn):
    # The CLI reply path (the push plugin uses it) must follow the same rule as
    # the MCP tool: the pairing is the consent for a remote agent.
    make_session(conn, "A", handle="alice", project="/repo/alpha", project_name="alpha")
    store.replace_remote_sessions(
        conn, "nodeB", [{"session_id": "R", "handle": "rita", "project_name": "gamma"}]
    )
    assert cli.main(["send", "--session", "A", "--to", "R", "--json", "--", "hi"]) == 0
    row = conn.execute("SELECT to_session FROM messages WHERE body='hi'").fetchone()
    assert row["to_session"] == "R"


def test_cli_send_between_roommates_of_different_projects_is_allowed(conn):
    make_session(conn, "A", handle="alice", project="/repo/alpha", project_name="alpha")
    make_session(conn, "B", handle="bob", project="/repo/beta", project_name="beta")
    assert cli.main(["send", "--session", "A", "--to", "bob", "--json", "--", "hi"]) == 0
