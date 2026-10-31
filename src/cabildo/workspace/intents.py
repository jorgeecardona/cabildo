"""The intents (push queue and its recent results) of the bus, for the panel's left side."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from cabildo import names, store

# a finished intent stays on the panel this long, so a failure is seen before it scrolls away
RECENT_SECONDS = 3600.0
RECENT_MAX = 3
QUEUE_STATUSES = ("running", "pending")


@dataclass(frozen=True)
class Intent:
    """One row of ``intents``: what was asked, by whom, and where it stands."""

    id: int
    action: str
    status: str  # pending | running | done | failed | rejected | expired
    project: str  # the repo the action is about
    by: str  # the asking session's handle, else its robot name
    at: float  # when it was queued (unix seconds)
    reason: str  # why it failed or was rejected; empty otherwise

    @property
    def waiting(self) -> bool:
        """True while the intent is in the queue (pending or running)."""
        return self.status in QUEUE_STATUSES


def _project(project: str | None, params: str | None) -> str:
    """The repo name: the ``cwd`` the action runs in, else the asking session's project."""
    try:
        cwd = (json.loads(params) if params else {}).get("cwd")
    except (ValueError, AttributeError):
        cwd = None
    path = str(cwd or project or "")
    return os.path.basename(path.rstrip("/")) or path


def _reason(result: str | None) -> str:
    """The first line of the failure reason in a result, or an empty string."""
    try:
        reason = (json.loads(result) if result else {}).get("reason")
    except (ValueError, AttributeError):
        return ""
    return str(reason).strip().splitlines()[0] if reason else ""


def _intent(r: sqlite3.Row) -> Intent:
    return Intent(
        id=r["id"],
        action=r["action"],
        status=r["status"],
        project=_project(r["project"], r["params"]),
        by=r["from_handle"] or names.robot_name(r["from_session"]),
        at=r["created_at"],
        reason=_reason(r["result"]),
    )


def load(now: float) -> tuple[Intent, ...]:
    """The queue (running first, then pending, oldest first), then the latest finished ones.

    Finished means done, failed, rejected or expired in the last ``RECENT_SECONDS``, at most
    ``RECENT_MAX``, newest first. Reads the DB read-only and never creates it. Returns an empty
    tuple when there is no DB or it cannot be read: the panel works without a bus.
    """
    path = store.db_path()
    if not path.exists():
        return ()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            queue = conn.execute(
                "SELECT * FROM intents WHERE status IN ('running','pending') "
                "ORDER BY status='pending', id"
            ).fetchall()
            finished = conn.execute(
                "SELECT * FROM intents WHERE status NOT IN ('running','pending') "
                "AND COALESCE(finished_at, created_at) >= ? ORDER BY id DESC LIMIT ?",
                (now - RECENT_SECONDS, RECENT_MAX),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return ()
    return tuple(_intent(r) for r in (*queue, *finished))
