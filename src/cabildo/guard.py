"""Decision logic for the PreToolUse guard hook: turn advisory claims into
enforced ones at the tool-call boundary.

Three conventions are enforced, all scoped to the session's own project:

  * ``push:<project_name>`` — a `git push` from any other session is denied
    while the lease is live.
  * ``file:<path>`` — an Edit/Write/NotebookEdit on a matching path is denied.
    Paths are matched repo-relative and absolute, with fnmatch globbing so a
    claim like ``file:cabildo/hooks/*`` covers the directory. And, crucially,
    an *allowed* edit auto-takes a short lease on that path (``autoclaim_on_edit``)
    so the guard has something to enforce without any agent remembering to
    claim — the first agent to touch a file owns it briefly; the next is denied.
  * destructive git in a shared working tree — ``git reset --hard``, a
    discarding ``checkout``/``restore``, ``clean -f``, ``stash`` — is denied
    while any *other* agent is active on the project, because those wipe the
    whole tree's uncommitted work, not just one claimed file.

Deny only ever fires for a claim held by *someone else* and still unexpired (or
for a tree-wide git op with live peers); everything else falls through to Claude
Code's normal permission flow. The reason string names the holder/peers so the
denied agent can go negotiate on the bus instead of retrying blind.
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

# Working-tree-destroying git ops. Each matches only as its own shell command
# (start or after ;&|), so the same words inside a quoted string don't trip it.
# These clobber *everyone's* uncommitted work in a shared checkout, which is the
# concrete harm we saw ("the reset that dropped feaf332a").
_DESTRUCTIVE_GIT = [
    (re.compile(r"(?:^|[;&|]\s*)git\s+reset\s+(?:[^;&|]*\s)?--hard\b"),
     "git reset --hard"),
    # checkout/restore that discards changes: `-- <path>`, `.`, or bare restore
    # (restore discards by default unless it's --staged-only).
    (re.compile(r"(?:^|[;&|]\s*)git\s+checkout\s+(?:[^;&|]*\s)?(?:--\s|\.(?:\s|$))"),
     "git checkout (discard)"),
    (re.compile(r"(?:^|[;&|]\s*)git\s+restore\b(?![^;&|]*--staged)"),
     "git restore (discard)"),
    (re.compile(r"(?:^|[;&|]\s*)git\s+clean\s+(?:[^;&|]*\s)?-[a-zA-Z]*f"),
     "git clean -f"),
    (re.compile(r"(?:^|[;&|]\s*)git\s+stash\b(?!\s+(?:list|show|pop|apply))"),
     "git stash"),
]

_FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}

# How long an auto-claim taken on edit lasts. Short, and refreshed on every
# re-edit, so an agent that moves on releases the file within minutes without
# needing to remember; SessionEnd drops it immediately.
AUTOCLAIM_TTL = 600.0


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


def _deny_destructive_git(
    conn: sqlite3.Connection, me: sqlite3.Row, command: str
) -> str | None:
    """Deny a working-tree-destroying git op while other agents are live on the
    project. Unlike push/file locks this is not tied to a claim: the op wipes
    the whole shared tree, so the trigger is simply 'someone else is here'."""
    label = next((lbl for rx, lbl in _DESTRUCTIVE_GIT if rx.search(command)), None)
    if label is None:
        return None
    peers = store.active_sessions(conn, project=me["project"], exclude=me["session_id"])
    if not peers:
        return None
    who = ", ".join(store.display_name(p) for p in peers[:5])
    return (
        f"cabildo: `{label}` in a shared working tree with {len(peers)} other "
        f"active agent(s) ({who}) can wipe their uncommitted work. Coordinate "
        "via `send`, or isolate in your own git worktree (`git worktree add`). "
        "Re-run once no one else is working the tree."
    )


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
        command = tool_input.get("command") or ""
        return _deny_push(conn, me, command) or _deny_destructive_git(conn, me, command)
    key = _FILE_TOOLS.get(tool_name)
    if key:
        path = tool_input.get(key)
        if path:
            return _deny_file(conn, me, str(path))
    return None


def _file_resource(me: sqlite3.Row, path: str) -> str:
    """The claim resource for a path: repo-relative when inside the project
    (portable, matches how humans claim `file:src/x.py`), else absolute."""
    project = me["project"]
    p = str(PurePosixPath(path))
    if project and p.startswith(project.rstrip("/") + "/"):
        return "file:" + p[len(project.rstrip("/")) + 1:]
    return "file:" + p


def autoclaim_on_edit(
    conn: sqlite3.Connection,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    """Take/refresh a short lease on a file this agent is *allowed* to edit, so
    the next agent to touch it hits _deny_file. Call this only after
    deny_reason returned None. Best-effort; returns the resource it claimed.

    This is what gives the guard teeth without agent discipline: claims were
    almost never made by hand, so nothing was ever enforced. Editing a file now
    *is* claiming it."""
    key = _FILE_TOOLS.get(tool_name)
    if not key:
        return None
    path = tool_input.get(key)
    if not path:
        return None
    me = store.get_session(conn, session_id)
    if me is None or not me["project"]:
        return None
    resource = _file_resource(me, str(path))
    store.acquire_claim(
        conn, resource=resource, holder_session=session_id,
        holder_handle=me["handle"], project=me["project"],
        ttl_seconds=AUTOCLAIM_TTL, note="auto: editing",
    )
    return resource
