"""The intents (push queue and its recent results) of the bus, for the panel's left side."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from cabildo import cloud, names, store

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
    project: str  # the repo name the action is about
    cwd: str  # the repo path the action runs in ('' when the intent has none)
    key: str  # the project key of ``cwd``, which is what a scope compares
    by: str  # the asking session's handle, else its robot name
    at: float  # when it was queued (unix seconds)
    reason: str  # why it failed or was rejected; empty otherwise

    @property
    def waiting(self) -> bool:
        """True while the intent is in the queue (pending or running)."""
        return self.status in QUEUE_STATUSES


def _path(project: str | None, params: str | None) -> str:
    """The repo path: the ``cwd`` the action runs in, else the asking session's project."""
    try:
        cwd = (json.loads(params) if params else {}).get("cwd")
    except (ValueError, AttributeError):
        cwd = None
    return str(cwd or project or "")


def _reason(result: str | None) -> str:
    """The first line of the failure reason in a result, or an empty string."""
    try:
        reason = (json.loads(result) if result else {}).get("reason")
    except (ValueError, AttributeError):
        return ""
    return str(reason).strip().splitlines()[0] if reason else ""


def _intent(r: sqlite3.Row) -> Intent:
    path = _path(r["project"], r["params"])
    return Intent(
        id=r["id"],
        action=r["action"],
        status=r["status"],
        project=os.path.basename(path.rstrip("/")) or path,
        cwd=path,
        key=cloud.project_key(path) if path else "",
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


def in_scope(items: tuple[Intent, ...], scope: str, cwd: str, key: str) -> tuple[Intent, ...]:
    """The intents that belong to the workspace's scope, the one the session list uses.

    ``here``: the project of ``cwd`` (``key`` is its project key). ``subfolders``: that project
    and any repo under ``cwd``. ``all``: every intent.
    """
    if scope == "all":
        return items
    root = cwd.rstrip("/")
    return tuple(
        i
        for i in items
        if i.key == key
        or (scope == "subfolders" and (i.cwd == root or i.cwd.startswith(root + "/")))
    )
