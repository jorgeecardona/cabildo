"""Cross-project consent: grants, topic gating, and the discovery directory
(issue #26). The data layer only — enforcement wiring is tested through the MCP
tools.
"""

from __future__ import annotations

from cabildo import store
from tests.conftest import make_session


def test_grant_lifecycle_requested_granted_revoked(conn):
    gid = store.upsert_grant(
        conn,
        grantee="crm",
        resource_kind="project",
        resource="paas",
        capability="send",
        status="requested",
        requested_by="s-crm",
    )
    # A mere request grants nothing.
    assert store.has_grant(conn, "crm", "project", "paas", "send") is False
    store.decide_grant(conn, gid, status="granted", by="s-paas")
    assert store.has_grant(conn, "crm", "project", "paas", "send") is True
    # Directional: the reverse is not implied.
    assert store.has_grant(conn, "paas", "project", "crm", "send") is False
    # And the other capability is not implied either.
    assert store.has_grant(conn, "crm", "project", "paas", "listen") is False
    store.decide_grant(conn, gid, status="revoked", by="s-paas")
    assert store.has_grant(conn, "crm", "project", "paas", "send") is False


def test_grant_is_case_insensitive_for_project_and_topic(conn):
    store.upsert_grant(
        conn,
        grantee="CRM",
        resource_kind="topic",
        resource="War-Room",
        capability="listen",
        status="granted",
        by="s-paas",
    )
    assert store.has_grant(conn, "crm", "topic", "war-room", "listen") is True


def test_rerequest_updates_the_same_row(conn):
    a = store.upsert_grant(
        conn,
        grantee="crm",
        resource_kind="project",
        resource="paas",
        capability="send",
        status="requested",
        requested_by="s1",
    )
    b = store.upsert_grant(
        conn,
        grantee="crm",
        resource_kind="project",
        resource="paas",
        capability="send",
        status="requested",
        requested_by="s1",
    )
    assert a == b
    assert len(store.list_grants(conn, grantee="crm")) == 1


def test_list_grants_filters(conn):
    store.upsert_grant(
        conn,
        grantee="crm",
        resource_kind="project",
        resource="paas",
        capability="send",
        status="granted",
        by="x",
    )
    store.upsert_grant(
        conn,
        grantee="crm",
        resource_kind="project",
        resource="paas",
        capability="listen",
        status="requested",
        requested_by="y",
    )
    assert len(store.list_grants(conn, grantee="crm")) == 2
    assert len(store.list_grants(conn, grantee="crm", status="granted")) == 1
    assert len(store.list_grants(conn, grantee="crm", status="requested")) == 1


def test_topic_gating_defaults_open(conn):
    store.ensure_topic(conn, "general")
    assert store.topic_is_gated(conn, "general") is False
    store.set_topic_gated(conn, "war-room", True)
    assert store.topic_is_gated(conn, "war-room") is True
    gated = {t["name"]: t["gated"] for t in store.list_topics(conn)}
    assert gated["war-room"] is True
    assert gated["general"] is False


def test_directory_lists_active_projects_without_messages(conn):
    make_session(conn, "a1", project="/r/crm", project_name="crm")
    make_session(conn, "a2", project="/r/crm", project_name="crm")
    make_session(conn, "b1", project="/r/paas", project_name="paas")
    store.set_role(conn, "a1", "reviewer", "reviews PRs")
    d = {row["project"].lower(): row for row in store.directory(conn)}
    assert d["crm"]["agents"] == 2
    assert d["paas"]["agents"] == 1
    assert "reviewer" in d["crm"]["roles"]
    # Discovery, not delivery: no message/body field is exposed.
    assert all("body" not in row and "messages" not in row for row in store.directory(conn))
