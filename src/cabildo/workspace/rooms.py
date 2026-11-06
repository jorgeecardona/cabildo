"""The project and topics (rooms) of a session, read from the bus DB (D-1)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from cabildo import names, store


@dataclass(frozen=True)
class Rooms:
    """Where a session sits on the bus."""

    project: str | None
    topics: tuple[str, ...]
    # (topic, scope) for the topics whose posts go to paired machines; a topic not
    # listed is local (docs/channels.md)
    scopes: tuple[tuple[str, str], ...] = ()
    agent: str = ""  # the session's robot name on the bus


NO_ROOMS = Rooms(project=None, topics=())

# ids per query: under the 999-variable limit of SQLite before 3.32
CHUNK = 500


def label(r: Rooms) -> str:
    """The topics as ``#general #lan(shared) #proj:cabildo``, or an empty string for none.

    A topic whose posts go to paired machines shows its scope in parentheses, except scope
    ``project`` on a ``proj:`` topic, where the name already says it.
    """
    scope = dict(r.scopes)

    def one(t: str) -> str:
        s = scope.get(t)
        return f"#{t}({s})" if s and not (s == "project" and t.startswith("proj:")) else f"#{t}"

    return " ".join(one(t) for t in r.topics)


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
    scopes = _scopes(conn)
    out: dict[str, Rooms] = {}
    for sid in projects:
        mine = tuple(sorted(topics.get(sid, [])))
        away = tuple((t, scopes[t]) for t in mine if scopes.get(t, "local") != "local")
        out[sid] = Rooms(projects[sid], mine, away, names.robot_name(sid))
    return out


def _scopes(conn: sqlite3.Connection) -> dict[str, str]:
    """Scope by topic name. Empty for a DB from before the ``scope`` column."""
    try:
        return dict(conn.execute("SELECT name, scope FROM topics"))
    except sqlite3.OperationalError:
        return {}
