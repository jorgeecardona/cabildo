"""A file that keeps the names you gave sessions, so they survive a lost machine.

A robot name such as ``tiny-dynamo-616b8`` is computed from the session id, so it needs no backup.
What cannot be computed is what you set: the ``@handle`` and role (kept in the bus database) and
the title, group and tags (kept in the workspace sidecar). This module writes those to one JSON
file and reads them back.

The daemon rewrites the file when something changed (``sync``). Put it in a folder you back up or
sync with ``[names] file = "~/Sync/cabildo-names.json"`` in ``~/.cabildo/config.toml``. On a new
machine, ``cabildo names import`` puts the names back. Import fills what is empty and never
overwrites a name you already set.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, names, store
from .workspace import sessions as ws

VERSION = 1
SYNC_EVERY_S = 60.0


class NamesFileError(ValueError):
    """The names file cannot be read as a names file."""


@dataclass(frozen=True)
class BusName:
    """What the bus database keeps for one session that you named."""

    session_id: str
    handle: str | None = None
    role: str | None = None
    responsibilities: str | None = None
    cwd: str | None = None
    project_name: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """Every name the machine cannot compute again."""

    sessions: tuple[BusName, ...] = ()
    meta: tuple[tuple[str, ws.Meta], ...] = ()  # (session id or chain key, title/group/tags)


@dataclass(frozen=True)
class Restored:
    """What an import changed."""

    added: int = 0  # sessions the bus did not know, now present as ended
    filled: int = 0  # handles, roles and sidecar entries that were empty
    kept: int = 0  # names that differ and were left as they are


def path() -> Path:
    """Where the names file lives: ``[names] file``, else ``names.json`` in the cabildo home."""
    return config.names_file() or store.home() / "names.json"


def capture(conn: sqlite3.Connection) -> Snapshot:
    """The names now: sessions with a handle or role, and the workspace sidecar."""
    rows = conn.execute(
        "SELECT session_id, handle, role, responsibilities, cwd, project_name FROM sessions "
        "WHERE handle IS NOT NULL OR role IS NOT NULL ORDER BY created_at, session_id"
    ).fetchall()
    sessions = tuple(BusName(*(r[k] for k in r.keys())) for r in rows)  # noqa: SIM118
    meta = tuple(sorted(ws.load_meta().items()))
    return Snapshot(sessions, tuple((k, m) for k, m in meta if _worth_keeping(m)))


def _worth_keeping(meta: ws.Meta) -> bool:
    return bool(meta.label or meta.group or meta.tags or meta.combined_from)


def render(snap: Snapshot, saved_at: float | None = None) -> str:
    """The JSON text of ``snap``. ``name`` is informational: the robot name of the id."""
    doc: dict[str, Any] = {
        "version": VERSION,
        "saved_at": time.time() if saved_at is None else saved_at,
        "sessions": [{"name": names.robot_name(s.session_id)} | _present(s) for s in snap.sessions],
        "meta": {
            key: {"label": m.label, "group": m.group, "tags": list(m.tags)}
            | ({"combined_from": list(m.combined_from)} if m.combined_from else {})
            for key, m in snap.meta
        },
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _present(s: BusName) -> dict[str, Any]:
    return {k: v for k, v in s.__dict__.items() if v is not None}


def parse(text: str) -> Snapshot:
    """The ``Snapshot`` in a names file's text.

    Raises:
        NamesFileError: when the text is not JSON, has another version, or has the wrong shape.
    """
    try:
        doc = json.loads(text)
    except ValueError as exc:
        msg = f"not JSON: {exc}"
        raise NamesFileError(msg) from exc
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        msg = f"not a version {VERSION} names file"
        raise NamesFileError(msg)
    sessions: list[BusName] = []
    for row in doc.get("sessions", []):
        sid = row.get("session_id") if isinstance(row, dict) else None
        if not isinstance(sid, str) or not sid:
            msg = "a session entry has no session_id"
            raise NamesFileError(msg)
        text_fields = {
            k: row[k]
            for k in ("handle", "role", "responsibilities", "cwd", "project_name")
            if isinstance(row.get(k), str)
        }
        sessions.append(BusName(sid, **text_fields))
    raw_meta = doc.get("meta", {})
    if not isinstance(raw_meta, dict):
        msg = "meta is not an object"
        raise NamesFileError(msg)
    meta = tuple(sorted(ws.meta_from_raw(raw_meta).items()))
    return Snapshot(tuple(sessions), meta)


def write(snap: Snapshot, target: Path | None = None, *, force: bool = False) -> bool:
    """Write the names file atomically. False when nothing changed since the last write.

    ``saved_at`` is left out of the comparison, so an idle machine does not rewrite the file.
    """
    target = target or path()
    new = render(snap, saved_at=0.0)
    if not force and target.exists():
        try:
            if render(parse(target.read_text(encoding="utf-8")), saved_at=0.0) == new:
                return False
        except (OSError, NamesFileError):
            pass  # unreadable or foreign: write ours
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(render(snap), encoding="utf-8")
    os.replace(tmp, target)
    return True


def sync(conn: sqlite3.Connection, log: Any) -> None:
    """Rewrite the names file when a name changed. Called by the daemon."""
    try:
        if write(capture(conn)):
            log(f"names: saved to {path()}")
    except OSError as exc:  # a full disk or a missing sync folder must not stop the daemon
        log(f"names: cannot write {path()}: {exc}")


def restore(conn: sqlite3.Connection, snap: Snapshot) -> Restored:
    """Put the names of ``snap`` back. Empty fields are filled; set fields are never overwritten."""
    added = filled = kept = 0
    now = time.time()
    for s in snap.sessions:
        row = conn.execute(
            "SELECT handle, role, responsibilities FROM sessions WHERE session_id=?",
            (s.session_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sessions(session_id, handle, role, responsibilities, cwd, "
                "project_name, client, status, created_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,'claude','ended',?,?)",
                (
                    s.session_id,
                    s.handle,
                    s.role,
                    s.responsibilities,
                    s.cwd,
                    s.project_name,
                    now,
                    now,
                ),
            )
            added += 1
            continue
        for column, value in (
            ("handle", s.handle),
            ("role", s.role),
            ("responsibilities", s.responsibilities),
        ):
            if value is None:
                continue
            if row[column] is None:
                conn.execute(
                    f"UPDATE sessions SET {column}=? WHERE session_id=?",
                    (value, s.session_id),
                )
                filled += 1
            elif row[column] != value:
                kept += 1
    conn.commit()
    meta_filled, meta_kept = _restore_meta(snap)
    return Restored(added, filled + meta_filled, kept + meta_kept)


def _restore_meta(snap: Snapshot) -> tuple[int, int]:
    filled = kept = 0
    with ws.meta_locked():
        current = ws.load_meta()
        for key, saved in snap.meta:
            have = current.get(key, ws.Meta())
            merged = ws.Meta(
                label=have.label or saved.label,
                group=have.group or saved.group,
                tags=have.tags or saved.tags,
                combined_from=have.combined_from or saved.combined_from,
            )
            if merged != have:
                current[key] = merged
                filled += 1
            if (have.label and saved.label and have.label != saved.label) or (
                have.group and saved.group and have.group != saved.group
            ):
                kept += 1
        if filled:
            ws.save_meta(current)
    return filled, kept
