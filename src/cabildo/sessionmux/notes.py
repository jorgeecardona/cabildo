"""Notes the user types in the panel for a session, kept in the bus DB (no model call).

A note is an open ``session_notes`` row with ``source='human'``. The panel shows them on
the session's row and edits them like tags: the whole list as one line, separated by ``;``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from cabildo import store

SEPARATOR = ";"
# ids per query: under the 999-variable limit of SQLite before 3.32
CHUNK = 500


@dataclass(frozen=True)
class Note:
    """One open note: its id in ``session_notes`` and its text."""

    id: int
    text: str


def label(notes: Iterable[Note]) -> str:
    """The note texts as one line (``a; b``), or an empty string for none."""
    return f"{SEPARATOR} ".join(n.text for n in notes)


def split(line: str) -> tuple[str, ...]:
    """The notes in an edited line: non-blank parts, trimmed, in order."""
    return tuple(p.strip() for p in line.split(SEPARATOR) if p.strip())


def load(session_ids: Iterable[str]) -> dict[str, tuple[Note, ...]]:
    """Open human notes by session id, oldest first.

    Reads the DB read-only and never creates it. Returns an empty dict when there are no ids,
    no DB, or the DB cannot be read: the panel works without a bus.
    """
    ids = list(dict.fromkeys(session_ids))
    path = store.db_path()
    if not ids or not path.exists():
        return {}
    found: dict[str, list[Note]] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            for start in range(0, len(ids), CHUNK):
                chunk = ids[start : start + CHUNK]
                marks = ",".join("?" * len(chunk))
                for sid, nid, text in conn.execute(
                    "SELECT session_id, id, text FROM session_notes "
                    f"WHERE source='human' AND state='open' AND session_id IN ({marks}) "
                    "ORDER BY id",
                    chunk,
                ):
                    found.setdefault(sid, []).append(Note(nid, text))
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {sid: tuple(notes) for sid, notes in found.items()}


def save(current: Iterable[Note], head_id: str, line: str) -> bool:
    """Make the open notes of a conversation equal ``line``; False when the DB is not writable.

    A note that is still in ``line`` keeps its id. One that is gone is marked done. A new
    text becomes a note of ``head_id``, the newest session of the conversation.
    """
    wanted = list(split(line))
    try:
        with store.connect() as conn:
            for note in current:
                if note.text in wanted:
                    wanted.remove(note.text)
                else:
                    store.set_note_state(conn, note.id, state="done")
            me = store.get_session(conn, head_id)
            for text in wanted:
                store.add_note(
                    conn,
                    text=text,
                    session_id=head_id,
                    handle=me["handle"] if me else None,
                    project=me["project"] if me else None,
                    project_name=me["project_name"] if me else None,
                    source="human",
                )
    except (sqlite3.Error, OSError):
        return False
    return True
