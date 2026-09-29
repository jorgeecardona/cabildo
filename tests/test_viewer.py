"""The local viewer reads the same bus the agents write to, read-only."""

from __future__ import annotations

import json

from cabildo import store, viewer
from tests.conftest import make_session


def test_state_shape_is_json_serializable(conn):
    make_session(conn, "a", handle="alice")
    s = viewer._state()
    for key in ("host", "db", "generated", "agents", "claims", "messages", "messages_total"):
        assert key in s
    json.dumps(s)  # must not raise


def test_agents_carry_name_and_ago(conn):
    make_session(conn, "a", handle="alice", project_name="alpha")
    s = viewer._state()
    names = {a["name"] for a in s["agents"]}
    assert "@alice" in names
    a = next(x for x in s["agents"] if x["name"] == "@alice")
    assert a["project"] == "alpha"
    assert isinstance(a["ago"], int) and a["ago"] >= 0


def test_message_to_field_channel_and_dm(conn):
    make_session(conn, "a", handle="alice")
    make_session(conn, "b", handle="bob")
    store.post_message(
        conn, from_session="a", from_handle="alice", body="hello channel", topic="general"
    )
    store.post_message(conn, from_session="a", from_handle="alice", body="hi bob", to_session="b")
    s = viewer._state()
    tos = {m["to"] for m in s["messages"]}
    assert "#general" in tos  # channel message -> #topic
    assert "@bob" in tos  # direct message -> recipient display name
    assert s["messages_total"] == 2


def test_claims_are_listed(conn):
    make_session(conn, "a", handle="alice")
    store.acquire_claim(
        conn,
        resource="push:repo",
        holder_session="a",
        holder_handle="alice",
        project=None,
        ttl_seconds=300,
        note="pushing",
    )
    s = viewer._state()
    assert len(s["claims"]) == 1
    c = s["claims"][0]
    assert c["resource"] == "push:repo"
    assert c["holder"] == "@alice"
    assert c["note"] == "pushing"
