"""Role questions: a ``to_role`` row, answered by a DM with ``reply_to``."""

from __future__ import annotations

import pytest

from cabildo import lan, questions, store
from tests.conftest import make_session


def _ask(conn, sid, role="product", body="what is the priority?", project="alpha"):
    store.post_message(
        conn, from_session=sid, from_handle=None, body=body, to_role=role, project=project
    )
    return conn.execute("SELECT guid FROM messages WHERE to_role IS NOT NULL").fetchone()["guid"]


def _answer(conn, sid, to, guid, body="ship A first"):
    return store.post_message(
        conn,
        from_session=sid,
        from_handle=None,
        body=body,
        to_session=to,
        reply_to=guid,
        project="alpha",
    )


def _bodies(conn, sid):
    return [m["body"] for m in store.inbox(conn, sid)]


def test_holder_sees_question_non_holder_does_not(conn):
    make_session(conn, "asker")
    make_session(conn, "pm")
    make_session(conn, "dev")
    store.set_role(conn, "pm", "Product", None)
    store.set_role(conn, "dev", "backend", None)
    _ask(conn, "asker")
    assert _bodies(conn, "pm") == ["what is the priority?"]
    assert _bodies(conn, "dev") == []
    assert _bodies(conn, "asker") == []


def test_question_waits_until_a_session_takes_the_role(conn):
    make_session(conn, "asker")
    make_session(conn, "late")
    _ask(conn, "asker")
    assert _bodies(conn, "late") == []
    store.set_role(conn, "late", "product", None)
    assert _bodies(conn, "late") == ["what is the priority?"]


def test_answer_removes_question_from_other_holders_and_reaches_asker(conn):
    make_session(conn, "asker")
    make_session(conn, "pm1")
    make_session(conn, "pm2")
    store.set_role(conn, "pm1", "product", None)
    store.set_role(conn, "pm2", "product", None)
    guid = _ask(conn, "asker")
    _answer(conn, "pm1", "asker", guid)
    assert _bodies(conn, "pm2") == []
    assert _bodies(conn, "asker") == ["ship A first"]
    q = questions.get(conn, guid)
    assert q is not None and q.answered_by is not None
    assert q.answered_by.by_session == "pm1"


def test_open_question_has_no_answers(conn):
    make_session(conn, "asker")
    guid = _ask(conn, "asker")
    q = questions.get(conn, guid)
    assert q is not None and q.answers == () and q.answered_by is None
    assert [x.guid for x in questions.open_for_role(conn, "PRODUCT", "alpha")] == [guid]
    assert [x.guid for x in questions.asked_by(conn, "asker")] == [guid]


def test_other_project_does_not_see_question(conn):
    make_session(conn, "asker")
    make_session(conn, "pm", project="/repo/beta", project_name="beta")
    store.set_role(conn, "pm", "product", None)
    _ask(conn, "asker")
    assert _bodies(conn, "pm") == []


def test_exactly_one_address(conn):
    with pytest.raises(ValueError):
        store.post_message(
            conn, from_session="a", from_handle=None, body="x", topic="t", to_role="product"
        )
    with pytest.raises(ValueError):
        store.post_message(conn, from_session="a", from_handle=None, body="x")


def test_project_key_matches_across_different_paths(conn):
    """A synced question carries the project key, so a holder whose checkout lives
    at another path still sees it.
    """
    make_session(conn, "pm", project="/home/b/alpha", project_name="alpha")
    store.set_role(conn, "pm", "product", None)
    store.post_message(
        conn, from_session="lan:x", from_handle=None, body="q?", to_role="product", project="alpha"
    )
    assert _bodies(conn, "pm") == ["q?"]


def test_open_for_role_without_project_matches_only_projectless(conn):
    make_session(conn, "asker")
    _ask(conn, "asker")
    assert questions.open_for_role(conn, "product", None) == []


def test_blank_role_is_refused(conn):
    with pytest.raises(ValueError):
        store.post_message(conn, from_session="a", from_handle=None, body="x", to_role="  ")


def test_role_key_matches_sqlite_lower_trim(conn):
    for role in ["Product", "  product ", "RÉVISOR", "\tx\n"]:
        sql = conn.execute("SELECT lower(trim(?))", (role,)).fetchone()[0]
        assert store.role_key(role) == sql


def test_non_ascii_role_is_found_by_the_inbox(conn):
    make_session(conn, "asker")
    make_session(conn, "rev")
    store.set_role(conn, "rev", "RÉVISOR", None)
    store.post_message(
        conn, from_session="asker", from_handle=None, body="q?", to_role="RÉVISOR", project="alpha"
    )
    assert _bodies(conn, "rev") == ["q?"]


def test_lan_select_all_gates_questions_by_project(conn):
    make_session(conn, "asker")
    _ask(conn, "asker")
    peer = "p1"
    store.register_session(
        conn, "lan-agent", project_name="zeta", client=store.REMOTE_PREFIX + peer
    )

    def shipped(exclude, share):
        rows, _ = lan.select_all(conn, 0, exclude, peer, frozenset(share))
        return [r["guid"] for r in rows]

    assert shipped(set(), set()) == []
    assert len(shipped(set(), {"alpha"})) == 1
    assert shipped({"alpha"}, {"alpha"}) == []


def test_same_guid_twice_is_one_row(conn):
    for _ in range(2):
        store.post_message(
            conn, from_session="a", from_handle=None, body="x", to_role="Product", guid="g1"
        )
    rows = conn.execute("SELECT to_role FROM messages WHERE guid='g1'").fetchall()
    assert [r["to_role"] for r in rows] == ["product"]


def test_question_and_answer_survive_lan_sync(conn):
    make_session(conn, "asker")
    guid = _ask(conn, "asker")
    _answer(conn, "pm", "asker", guid)
    rows, _ = lan.select_outbound(conn, "alpha", 0)
    by_guid = {r["guid"]: r for r in rows}
    assert by_guid[guid]["to_role"] == "product"
    assert any(r["reply_to"] == guid for r in rows)
