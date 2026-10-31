"""Questions addressed to a role.

A question is one ``messages`` row with ``to_role`` set. An answer is a normal
direct message whose ``reply_to`` is the question's ``guid``. Open or answered is
derived from those rows and never stored, so two machines that sync the rows
always agree.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cabildo import store


@dataclass(frozen=True)
class Answer:
    """One reply to a question."""

    guid: str
    by_session: str
    by_handle: str | None
    at: float
    body: str


@dataclass(frozen=True)
class Question:
    """A question for a role, with its answers ordered by time.

    The question stays addressed to the role, not to the session that held it
    when the question was asked. An empty ``answers`` means the question is open.
    """

    guid: str
    role: str
    asked_by: str
    asked_handle: str | None
    asked_at: float
    body: str
    project: str | None
    answers: tuple[Answer, ...]

    @property
    def answered_by(self) -> Answer | None:
        """The earliest answer, or None while the question is open."""
        return self.answers[0] if self.answers else None


def _answers(conn: sqlite3.Connection, guid: str) -> tuple[Answer, ...]:
    rows = conn.execute(
        "SELECT guid, from_session, from_handle, created_at, body FROM messages "
        "WHERE reply_to=? ORDER BY created_at, guid",
        (guid,),
    ).fetchall()
    return tuple(
        Answer(r["guid"], r["from_session"], r["from_handle"], r["created_at"], r["body"])
        for r in rows
    )


def _question(conn: sqlite3.Connection, r: sqlite3.Row) -> Question:
    return Question(
        guid=r["guid"],
        role=r["to_role"],
        asked_by=r["from_session"],
        asked_handle=r["from_handle"],
        asked_at=r["created_at"],
        body=r["body"],
        project=r["project"],
        answers=_answers(conn, r["guid"]),
    )


def get(conn: sqlite3.Connection, guid: str) -> Question | None:
    """The question with this guid, or None when no such question exists."""
    r = conn.execute(
        "SELECT * FROM messages WHERE guid=? AND to_role IS NOT NULL", (guid,)
    ).fetchone()
    return _question(conn, r) if r else None


def asked_by(
    conn: sqlite3.Connection, session_id: str, *, open_only: bool = True
) -> list[Question]:
    """Questions this session asked, oldest first."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE from_session=? AND to_role IS NOT NULL ORDER BY created_at",
        (session_id,),
    ).fetchall()
    qs = [_question(conn, r) for r in rows]
    return [q for q in qs if not open_only or not q.answers]


def open_for_role(
    conn: sqlite3.Connection, role: str, project: str | None = None
) -> list[Question]:
    """Unanswered questions addressed to ``role`` in project key ``project``.

    Same predicate as the inbox: a ``None`` project matches only questions with no
    project, never every project.
    """
    rows = conn.execute(
        "SELECT m.* FROM messages m WHERE m.to_role=? AND m.project IS ? "
        "AND NOT EXISTS (SELECT 1 FROM messages a WHERE a.reply_to=m.guid) "
        "ORDER BY m.created_at",
        (store.role_key(role), project),
    ).fetchall()
    return [_question(conn, r) for r in rows]
