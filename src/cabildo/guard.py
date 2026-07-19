"""Decision logic for the PreToolUse guard hook: turn advisory claims into
enforced ones at the tool-call boundary.

Two conventions are enforced, both scoped to the session's own project:

  * ``push:<project_name>`` — a `git push` from any other session is denied
    while the lease is live.
  * ``file:<path>`` — an Edit/Write/NotebookEdit on a matching path is denied.
    Paths are matched repo-relative and absolute, with fnmatch globbing so a
    claim like ``file:cabildo/hooks/*`` covers the directory.

Deny only ever fires for a claim held by *someone else* and still unexpired;
everything else falls through to Claude Code's normal permission flow. The
reason string names the holder so the denied agent can go negotiate on the bus
instead of retrying blind.
"""

from __future__ import annotations

import re
import sqlite3
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

from . import store

# `git ... push ...` as its own shell command: start of string or after a
# separator, so `echo "git push"` in a commit message doesn't trip it.
_PUSH_RE = re.compile(r"(?:^|[;&|]\s*)git\s+(?:[^;&|]*\s)?push\b")

_FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}


def _held_by_other(
    conn: sqlite3.Connection, resource: str, session_id: str
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM claims WHERE resource=? AND expires_at>? AND holder_session!=?",
        (resource, store._now(), session_id),
    ).fetchone()
    return row


def _holder_name(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    return store.display_name(
        store.get_session(conn, row["holder_session"]), row["holder_session"]
    )


def _deny_push(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
    if not _PUSH_RE.search(command):
        return None
    resource = f"push:{me['project_name']}"
    row = _held_by_other(conn, resource, me["session_id"])
    if not row:
        return None
    note = f" ({row['note']})" if row["note"] else ""
    return (
        f"cabildo: {_holder_name(conn, row)} holds the lock `{resource}`{note}, "
        f"expiring in {round(row['expires_at'] - store._now())}s. Pushing now could "
        "clobber their in-flight work. Wait, or message them via `send`; take the "
        "lock with `claim` once it is free."
    )


def _deny_file(conn: sqlite3.Connection, me: sqlite3.Row, path: str) -> str | None:
    abspath = PurePosixPath(path)
    rel: str | None = None
    project = me["project"]
    if project and str(abspath).startswith(project.rstrip("/") + "/"):
        rel = str(abspath)[len(project.rstrip("/")) + 1:]
    for row in conn.execute(
        "SELECT * FROM claims WHERE resource LIKE 'file:%' AND expires_at>? "
        "AND holder_session!=? AND project=?",
        (store._now(), me["session_id"], project),
    ):
        pattern = row["resource"][len("file:"):]
        if fnmatch(str(abspath), pattern) or (rel is not None and fnmatch(rel, pattern)):
            note = f" ({row['note']})" if row["note"] else ""
            return (
                f"cabildo: {_holder_name(conn, row)} holds `{row['resource']}`{note}, "
                f"expiring in {round(row['expires_at'] - store._now())}s — this edit "
                f"would touch {rel or path}. Coordinate via `send` before editing, "
                "or wait for the lease to expire."
            )
    return None


def deny_reason(
    conn: sqlite3.Connection,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    """Reason to deny this tool call, or None to fall through. Never raises —
    a guard that crashes must not take the session down with it."""
    me = store.get_session(conn, session_id)
    if me is None or not me["project"]:
        return None
    if tool_name == "Bash" and me["project_name"]:
        return _deny_push(conn, me, tool_input.get("command") or "")
    key = _FILE_TOOLS.get(tool_name)
    if key:
        path = tool_input.get(key)
        if path:
            return _deny_file(conn, me, str(path))
    return None
