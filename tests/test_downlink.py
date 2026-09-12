"""The cloud→local downlink: replies typed into the hosted dashboard land on
the local bus, and — the part that actually bites — never bounce back up.

The mirror is otherwise one-way (local → cloud). Once messages can also flow
down, the uplink cursor alone stops being enough: an injected row gets a fresh
autoincrement id, so the very next uplink pass would push it back to the cloud
as a duplicate, and the pass after that would push the duplicate's duplicate.
The `origin` column is what breaks that cycle.
"""

from __future__ import annotations

from cabildo import store
from tests.conftest import make_session


def _uplink_batch(conn, since: int):
    """The query webapp/runner.py and cabildo.cloud use to mirror upward."""
    return conn.execute(
        "SELECT id, body FROM messages "
        "WHERE id>? AND COALESCE(origin,'local')<>'cloud' ORDER BY id",
        (since,),
    ).fetchall()


def test_messages_are_local_by_default(conn):
    make_session(conn, "s1", project_name="alpha")
    store.post_message(
        conn, from_session="s1", from_handle="a", body="hi", topic=store.project_topic("alpha")
    )
    assert conn.execute("SELECT origin FROM messages").fetchone()["origin"] == "local"


def test_cloud_message_is_delivered_but_never_mirrored_back(conn):
    make_session(conn, "s1", project_name="alpha")
    topic = store.project_topic("alpha")
    store.post_message(
        conn, from_session="s1", from_handle="a", body="should I use postgres?", topic=topic
    )

    # The uplink mirrors the agent's question and parks its cursor on it.
    batch = _uplink_batch(conn, 0)
    assert [r["body"] for r in batch] == ["should I use postgres?"]
    cursor = batch[-1]["id"]

    # Jorge answers in the browser; the runner injects it locally.
    store.post_message(
        conn,
        from_session="human:jorge",
        from_handle="jorge",
        body="yes, postgres",
        topic=topic,
        origin="cloud",
    )

    # It is a real message on the bus — the agent it answers must actually
    # receive it, which is the whole point of the downlink.
    assert [m["body"] for m in store.inbox(conn, "s1")] == ["yes, postgres"]

    # ...but the uplink must not carry it back to the cloud, where it already is.
    assert _uplink_batch(conn, cursor) == []


def test_downlink_does_not_strand_later_local_messages(conn):
    """A cloud reply sitting between two local messages must not shadow the
    one behind it — the naive fix (jump the cursor past the injected row)
    would silently drop it.
    """
    make_session(conn, "s1", project_name="alpha")
    topic = store.project_topic("alpha")
    store.post_message(conn, from_session="s1", from_handle="a", body="first", topic=topic)
    cursor = _uplink_batch(conn, 0)[-1]["id"]

    store.post_message(
        conn,
        from_session="human:jorge",
        from_handle="jorge",
        body="from the dashboard",
        topic=topic,
        origin="cloud",
    )
    store.post_message(conn, from_session="s1", from_handle="a", body="second", topic=topic)

    assert [r["body"] for r in _uplink_batch(conn, cursor)] == ["second"]


def test_origin_column_is_added_to_an_existing_bus(conn):
    """_migrate must reach databases created before the column existed —
    CREATE TABLE IF NOT EXISTS is a no-op against a live file.
    """
    conn.execute("ALTER TABLE messages RENAME TO messages_old")
    conn.execute(
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,"
        " from_session TEXT NOT NULL, from_handle TEXT, topic TEXT,"
        " to_session TEXT, project TEXT, body TEXT NOT NULL,"
        " urgent INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    assert "origin" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}

    store._migrate(conn)

    assert "origin" in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    make_session(conn, "s1", project_name="alpha")
    store.post_message(
        conn,
        from_session="s1",
        from_handle="a",
        body="after migration",
        topic=store.project_topic("alpha"),
    )
    assert conn.execute("SELECT origin FROM messages").fetchone()["origin"] == "local"
