"""The project and topics (rooms) of a session, read from the bus DB (D-1)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from cabildo import store


@dataclass(frozen=True)
class Rooms:
    """Where a session sits on the bus."""

    project: str | None
    topics: tuple[str, ...]


NO_ROOMS = Rooms(project=None, topics=())

# ids per query: under the 999-variable limit of SQLite before 3.32
CHUNK = 500


def label(r: Rooms) -> str:
    """The topics as ``#general #proj:cabildo``, or an empty string for none."""
    return " ".join(f"#{t}" for t in r.topics)


def load(session_ids: Iterable[str]) -> dict[str, Rooms]:
    """Rooms of the sessions the bus knows, by session id.

    Reads the DB read-only and never creates it. Returns an empty dict when there are no ids,
    no DB, or the DB cannot be read: the panel works without a bus.
    """
    ids = list(dict.fromkeys(session_ids))
    path = store.db_path()
    if not ids or not path.exists():
        return {}
    found: dict[str, Rooms] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            for start in range(0, len(ids), CHUNK):
                found.update(_load_chunk(conn, ids[start : start + CHUNK]))
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return found


def _load_chunk(conn: sqlite3.Connection, ids: list[str]) -> dict[str, Rooms]:
    """Rooms of one batch of ids, small enough for SQLite's variable limit."""
    marks = ",".join("?" * len(ids))
    projects = dict(
        conn.execute(
            f"SELECT session_id, project_name FROM sessions WHERE session_id IN ({marks})", ids
        )
    )
    topics: dict[str, list[str]] = {}
    for sid, topic in conn.execute(
        f"SELECT session_id, topic FROM subscriptions WHERE session_id IN ({marks})", ids
    ):
        topics.setdefault(sid, []).append(topic)
    return {sid: Rooms(projects[sid], tuple(sorted(topics.get(sid, [])))) for sid in projects}
