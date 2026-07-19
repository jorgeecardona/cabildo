"""SQLite-backed shared store for the cabildo IPC bus.

One DB file (default ~/.cabildo/chat.db) is shared by every Claude Code
session's MCP subprocess *and* by the hook scripts. WAL mode + a busy timeout
keep concurrent readers/writers from tripping over each other at this volume.

Everything is plain functions over a connection; no ORM, no globals beyond the
resolved paths. `time.time()` floats are used for all timestamps.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Paths / config
# --------------------------------------------------------------------------- #
def home() -> Path:
    env = os.environ.get("CABILDO_HOME") or os.environ.get("AGENT_CHAT_HOME")
    if env:
        return Path(env)
    p = Path(os.path.expanduser("~/.cabildo"))
    legacy = Path(os.path.expanduser("~/.agent-chat"))
    # Pre-rename bus: keep using it until the installer moves it, so history
    # and live sessions don't split across two databases.
    if not p.exists() and legacy.exists():
        return legacy
    return p


def db_path() -> Path:
    return home() / "chat.db"


DEFAULT_TOPIC = "general"
# A session is considered "active" if seen within this many seconds.
ACTIVE_WINDOW = 30 * 60

# The human's identity on the bus: a stable pseudo-session, so agents can address
# him exactly like they address each other.
HUMAN_SESSION = "human:jorge"
HUMAN_HANDLE = "jorge"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    handle        TEXT,
    cwd           TEXT,
    project       TEXT,
    project_name  TEXT,
    claude_pid    INTEGER,
    -- Set for spawned agents via CABILDO_TAG in the environment. The PID
    -- rendezvous guesses; a tag is told. Both the SessionStart hook and the MCP
    -- server inherit the same env from the same `claude`, so they agree by
    -- construction instead of by inference.
    tag           TEXT,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | ended
    created_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_pid ON sessions(claude_pid);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
-- idx_sessions_tag is created in _migrate(), not here: on an existing database
-- the CREATE TABLE above is a no-op, so indexing `tag` from this script throws
-- "no such column" before the migration has a chance to add it.

CREATE TABLE IF NOT EXISTS topics (
    name        TEXT PRIMARY KEY,
    project     TEXT,                 -- non-null for auto per-project channels
    created_by  TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    session_id  TEXT NOT NULL,
    topic       TEXT NOT NULL,
    joined_at   REAL NOT NULL,
    PRIMARY KEY (session_id, topic)
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,
    from_session  TEXT NOT NULL,
    from_handle   TEXT,
    topic         TEXT,               -- set => channel message
    to_session    TEXT,               -- set => direct message
    project       TEXT,
    body          TEXT NOT NULL,
    urgent        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_session);

CREATE TABLE IF NOT EXISTS reads (
    message_id  INTEGER NOT NULL,
    session_id  TEXT NOT NULL,
    read_at     REAL NOT NULL,
    PRIMARY KEY (message_id, session_id)
);

-- Mid-turn delivery receipts: messages a hook already surfaced into a session's
-- context. Delivery is NOT read — the agent still has to `read` them; this
-- table only stops the PostToolUse hook re-injecting the same message on every
-- subsequent tool call.
CREATE TABLE IF NOT EXISTS deliveries (
    message_id    INTEGER NOT NULL,
    session_id    TEXT NOT NULL,
    delivered_at  REAL NOT NULL,
    PRIMARY KEY (message_id, session_id)
);

-- Advisory leases: the coordination primitive (e.g. resource='push:<project>').
CREATE TABLE IF NOT EXISTS claims (
    resource        TEXT PRIMARY KEY,
    holder_session  TEXT NOT NULL,
    holder_handle   TEXT,
    project         TEXT,
    note            TEXT,
    acquired_at     REAL NOT NULL,
    expires_at      REAL NOT NULL
);

-- Control plane. Deliberately NOT chat: agents ask for an action by inserting a
-- structured row, and the daemon executes it. Natural language never reaches the
-- trust path, so the same request always means the same thing.
CREATE TABLE IF NOT EXISTS intents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,
    from_session  TEXT NOT NULL,
    from_handle   TEXT,
    project       TEXT,
    action        TEXT NOT NULL,
    params        TEXT NOT NULL,                   -- JSON object
    status        TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|rejected
    attempts      INTEGER NOT NULL DEFAULT 0,
    worker        TEXT,                            -- daemon instance that took it
    started_at    REAL,
    finished_at   REAL,
    result        TEXT                             -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status, id);

-- Spawn ledger: one row per agent the daemon launched. This is the birth
-- certificate — who asked (parentage), what for, and what became of it. It
-- lives in the DB and not in daemon memory because a restarted daemon must
-- still see the agents its predecessor launched, or every cap and depth limit
-- resets to zero on restart. `tag` is the identity handed to the child via
-- CABILDO_TAG before the child exists; `session_id` is backfilled when the
-- child's SessionStart hook registers carrying that tag.
CREATE TABLE IF NOT EXISTS spawns (
    tag            TEXT PRIMARY KEY,
    parent_session TEXT NOT NULL,
    role           TEXT NOT NULL,
    task           TEXT NOT NULL,
    model          TEXT,                              -- NULL = claude's default
    depth          INTEGER NOT NULL DEFAULT 1,        -- human/terminal parent = 0
    pid            INTEGER,
    worktree       TEXT,
    branch         TEXT,
    log            TEXT,
    session_id     TEXT,
    status         TEXT NOT NULL DEFAULT 'launched',  -- launched|registered|dead|ended
    created_at     REAL NOT NULL,
    ended_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_spawns_parent ON spawns(parent_session);
CREATE INDEX IF NOT EXISTS idx_spawns_session ON spawns(session_id);
CREATE INDEX IF NOT EXISTS idx_spawns_status ON spawns(status);
"""


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to tables that already exist.

    CREATE TABLE IF NOT EXISTS is a no-op against a live database, so a new
    column in SCHEMA silently never appears on anyone's actual bus — the schema
    would claim a shape the file doesn't have. Additive only; nothing here drops
    or rewrites data.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "tag" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN tag TEXT")
    # Unconditional: the column now exists either way (fresh via SCHEMA, old via
    # the ALTER above), and this is the only place it is safe to index it.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tag ON sessions(tag)")
    conn.commit()


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def ensure_topic(conn: sqlite3.Connection, name: str, project: str | None = None,
                 created_by: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO topics(name, project, created_by, created_at) "
        "VALUES (?,?,?,?)",
        (name, project, created_by, _now()),
    )


def subscribe(conn: sqlite3.Connection, session_id: str, topic: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions(session_id, topic, joined_at) VALUES (?,?,?)",
        (session_id, topic, _now()),
    )


def unsubscribe(conn: sqlite3.Connection, session_id: str, topic: str) -> None:
    conn.execute(
        "DELETE FROM subscriptions WHERE session_id=? AND topic=?", (session_id, topic)
    )


def project_topic(project_name: str) -> str:
    return f"proj:{project_name}"


def register_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    cwd: str | None = None,
    project: str | None = None,
    project_name: str | None = None,
    claude_pid: int | None = None,
    tag: str | None = None,
) -> None:
    """Idempotent upsert of a session + its default subscriptions. Called from
    the SessionStart hook (authoritative session_id) and, defensively, from the
    MCP server. Only overwrites columns we were actually given."""
    now = _now()
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sessions(session_id, cwd, project, project_name, claude_pid, "
            "tag, status, created_at, last_seen_at) VALUES (?,?,?,?,?,?, 'active', ?, ?)",
            (session_id, cwd, project, project_name, claude_pid, tag, now, now),
        )
    else:
        conn.execute(
            "UPDATE sessions SET "
            "cwd=COALESCE(?,cwd), project=COALESCE(?,project), "
            "project_name=COALESCE(?,project_name), claude_pid=COALESCE(?,claude_pid), "
            "tag=COALESCE(?,tag), status='active', last_seen_at=? WHERE session_id=?",
            (cwd, project, project_name, claude_pid, tag, now, session_id),
        )
    if tag:
        # Close the loop with the spawn ledger: the daemon recorded this tag
        # before the child existed; the child's registration is what proves the
        # launch actually became an agent.
        conn.execute(
            "UPDATE spawns SET session_id=?, status='registered' "
            "WHERE tag=? AND session_id IS NULL",
            (session_id, tag),
        )
    # An agent's default home is its OWN project channel — nothing else. #general
    # still exists as a fleet-wide room, but it is opt-in (join_topic): a CRM
    # agent must never receive paas chatter just by starting up. Isolation by
    # default; broadcasting on purpose.
    ensure_topic(conn, DEFAULT_TOPIC)  # keep the room available to join
    if project_name:
        pt = project_topic(project_name)
        ensure_topic(conn, pt, project=project, created_by=session_id)
        subscribe(conn, session_id, pt)
    conn.commit()


def touch(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET last_seen_at=?, status='active' WHERE session_id=?",
        (_now(), session_id),
    )
    conn.commit()


def end_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET status='ended', last_seen_at=? WHERE session_id=?",
        (_now(), session_id),
    )
    # Drop this session's advisory claims so nothing it held blocks others.
    conn.execute("DELETE FROM claims WHERE holder_session=?", (session_id,))
    # A spawned agent that ends cleanly closes its ledger row, freeing its slot
    # under the fleet cap without waiting for a PID liveness sweep.
    conn.execute(
        "UPDATE spawns SET status='ended', ended_at=? "
        "WHERE session_id=? AND status IN ('launched','registered')",
        (_now(), session_id),
    )
    conn.commit()


def set_handle(conn: sqlite3.Connection, session_id: str, handle: str) -> None:
    conn.execute(
        "UPDATE sessions SET handle=?, last_seen_at=? WHERE session_id=?",
        (handle, _now(), session_id),
    )
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()


def session_for_tag(conn: sqlite3.Connection, tag: str) -> str | None:
    """Resolve identity from the env tag a spawned agent was born with. Exact,
    unlike the PID walk: a spawned `claude -p` is not a terminal's descendant,
    and the ancestor search happily finds somebody else's claude."""
    if not tag:
        return None
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE tag=? ORDER BY last_seen_at DESC LIMIT 1",
        (tag,),
    ).fetchone()
    return row["session_id"] if row else None


def session_for_pid(conn: sqlite3.Connection, claude_pid: int) -> str | None:
    """Reverse the rendezvous: given the resolved claude PID, return the
    session_id the SessionStart hook bound to it (most-recently-seen wins)."""
    if not claude_pid:
        return None
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE claude_pid=? ORDER BY last_seen_at DESC "
        "LIMIT 1",
        (claude_pid,),
    ).fetchone()
    return row["session_id"] if row else None


def active_sessions(
    conn: sqlite3.Connection, project: str | None = None, exclude: str | None = None
) -> list[sqlite3.Row]:
    cutoff = _now() - ACTIVE_WINDOW
    q = "SELECT * FROM sessions WHERE status='active' AND last_seen_at>=?"
    args: list[Any] = [cutoff]
    if project:
        q += " AND project=?"
        args.append(project)
    if exclude:
        q += " AND session_id!=?"
        args.append(exclude)
    q += " ORDER BY last_seen_at DESC"
    return conn.execute(q, args).fetchall()


def display_name(row: sqlite3.Row | None, session_id: str | None = None) -> str:
    """Short human label for a session: @handle if set, else short id."""
    if row is not None:
        if row["handle"]:
            return f"@{row['handle']}"
        session_id = row["session_id"]
    if not session_id:
        return "?"
    return session_id[:8]


def subscriptions_of(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return [
        r["topic"]
        for r in conn.execute(
            "SELECT topic FROM subscriptions WHERE session_id=?", (session_id,)
        )
    ]


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def post_message(
    conn: sqlite3.Connection,
    *,
    from_session: str,
    from_handle: str | None,
    body: str,
    topic: str | None = None,
    to_session: str | None = None,
    project: str | None = None,
    urgent: bool = False,
) -> int:
    if (topic is None) == (to_session is None):
        raise ValueError("exactly one of topic / to_session must be set")
    cur = conn.execute(
        "INSERT INTO messages(created_at, from_session, from_handle, topic, to_session, "
        "project, body, urgent) VALUES (?,?,?,?,?,?,?,?)",
        (_now(), from_session, from_handle, topic, to_session, project, body,
         1 if urgent else 0),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _inbox_where(session_id: str) -> tuple[str, list[Any]]:
    """SQL predicate for 'messages addressed to me': DMs to me, or messages in
    a topic I'm subscribed to. Never my own messages."""
    return (
        "(m.to_session=? OR (m.topic IS NOT NULL AND m.topic IN "
        "(SELECT topic FROM subscriptions WHERE session_id=?))) "
        "AND m.from_session!=?",
        [session_id, session_id, session_id],
    )


def inbox(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    unread_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where, args = _inbox_where(session_id)
    q = (
        "SELECT m.*, r.read_at AS read_at FROM messages m "
        "LEFT JOIN reads r ON r.message_id=m.id AND r.session_id=? "
        f"WHERE {where}"
    )
    args = [session_id, *args]
    if unread_only:
        q += " AND r.read_at IS NULL"
    q += " ORDER BY m.created_at ASC LIMIT ?"
    args.append(limit)
    return [_msg_dict(r) for r in conn.execute(q, args)]


def unread_summary(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Compact aggregate used by the notification hooks."""
    rows = inbox(conn, session_id, unread_only=True, limit=200)
    by_sender: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    dm = 0
    urgent = 0
    for m in rows:
        if m["urgent"]:
            urgent += 1
        if m["to_session"]:
            dm += 1
        if m["topic"]:
            by_topic[m["topic"]] = by_topic.get(m["topic"], 0) + 1
        s = m["from_display"]
        by_sender[s] = by_sender.get(s, 0) + 1
    return {
        "count": len(rows),
        "dm": dm,
        "urgent": urgent,
        "by_sender": by_sender,
        "by_topic": by_topic,
    }


def mark_read(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    message_ids: Iterable[int] | None = None,
    topic: str | None = None,
    all_unread: bool = False,
) -> int:
    """Explicitly mark messages read. Returns number newly marked. Only ever
    marks messages that are actually in this session's inbox."""
    where, args = _inbox_where(session_id)
    q = f"SELECT m.id FROM messages m WHERE {where}"
    if message_ids is not None:
        ids = list(message_ids)
        if not ids:
            return 0
        q += f" AND m.id IN ({','.join('?' * len(ids))})"
        args += ids
    elif topic is not None:
        q += " AND m.topic=?"
        args.append(topic)
    elif not all_unread:
        return 0
    target = [r["id"] for r in conn.execute(q, args)]
    now = _now()
    n = 0
    for mid in target:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reads(message_id, session_id, read_at) VALUES (?,?,?)",
            (mid, session_id, now),
        )
        n += cur.rowcount
    conn.commit()
    return n


def undelivered_urgent(
    conn: sqlite3.Connection, session_id: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Unread URGENT messages that no mid-turn hook has surfaced yet. The
    PostToolUse hook injects these once and records them in `deliveries`."""
    where, args = _inbox_where(session_id)
    q = (
        "SELECT m.*, r.read_at AS read_at FROM messages m "
        "LEFT JOIN reads r ON r.message_id=m.id AND r.session_id=? "
        "LEFT JOIN deliveries d ON d.message_id=m.id AND d.session_id=? "
        f"WHERE {where} AND m.urgent=1 AND r.read_at IS NULL "
        "AND d.delivered_at IS NULL ORDER BY m.created_at ASC LIMIT ?"
    )
    return [_msg_dict(r) for r in conn.execute(q, [session_id, session_id, *args, limit])]


def mark_delivered(
    conn: sqlite3.Connection, session_id: str, message_ids: Iterable[int]
) -> None:
    now = _now()
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries(message_id, session_id, delivered_at) "
        "VALUES (?,?,?)",
        [(mid, session_id, now) for mid in message_ids],
    )
    conn.commit()


def history(
    conn: sqlite3.Connection,
    *,
    topic: str | None = None,
    between: tuple[str, str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Browse a conversation regardless of read state. `between` is an ordered
    pair of session_ids for a DM thread."""
    if topic is not None:
        rows = conn.execute(
            "SELECT * FROM messages WHERE topic=? ORDER BY created_at DESC LIMIT ?",
            (topic, limit),
        ).fetchall()
    elif between is not None:
        a, b = between
        rows = conn.execute(
            "SELECT * FROM messages WHERE (from_session=? AND to_session=?) "
            "OR (from_session=? AND to_session=?) ORDER BY created_at DESC LIMIT ?",
            (a, b, b, a, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_msg_dict(r) for r in reversed(rows)]


def _msg_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["urgent"] = bool(d.get("urgent"))
    d["from_display"] = f"@{r['from_handle']}" if r["from_handle"] else r["from_session"][:8]
    d["read"] = ("read_at" in d.keys()) and d.get("read_at") is not None
    return d


# --------------------------------------------------------------------------- #
# Topics
# --------------------------------------------------------------------------- #
def list_topics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.name, t.project, "
        "(SELECT COUNT(*) FROM subscriptions s WHERE s.topic=t.name) AS subscribers, "
        "(SELECT COUNT(*) FROM messages m WHERE m.topic=t.name) AS messages "
        "FROM topics t ORDER BY t.name"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Claims (advisory leases)
# --------------------------------------------------------------------------- #
def acquire_claim(
    conn: sqlite3.Connection,
    *,
    resource: str,
    holder_session: str,
    holder_handle: str | None,
    project: str | None,
    ttl_seconds: float,
    note: str | None,
) -> dict[str, Any]:
    """Try to take an exclusive lease on `resource`. Succeeds if free, expired,
    or already held by us (in which case the lease is refreshed)."""
    now = _now()
    row = conn.execute("SELECT * FROM claims WHERE resource=?", (resource,)).fetchone()
    if row and row["expires_at"] > now and row["holder_session"] != holder_session:
        return {
            "ok": False,
            "resource": resource,
            "held_by": display_name(get_session(conn, row["holder_session"]),
                                    row["holder_session"]),
            "held_by_session": row["holder_session"],
            "note": row["note"],
            "expires_in": round(row["expires_at"] - now, 1),
        }
    expires = now + ttl_seconds
    conn.execute(
        "INSERT INTO claims(resource, holder_session, holder_handle, project, note, "
        "acquired_at, expires_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(resource) DO UPDATE SET holder_session=excluded.holder_session, "
        "holder_handle=excluded.holder_handle, project=excluded.project, "
        "note=excluded.note, acquired_at=excluded.acquired_at, "
        "expires_at=excluded.expires_at",
        (resource, holder_session, holder_handle, project, note, now, expires),
    )
    conn.commit()
    return {"ok": True, "resource": resource, "expires_in": round(ttl_seconds, 1)}


def release_claim(conn: sqlite3.Connection, *, resource: str, holder_session: str) -> bool:
    cur = conn.execute(
        "DELETE FROM claims WHERE resource=? AND holder_session=?",
        (resource, holder_session),
    )
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Spawns (the lineage ledger)
# --------------------------------------------------------------------------- #
def record_spawn(
    conn: sqlite3.Connection,
    *,
    tag: str,
    parent_session: str,
    role: str,
    task: str,
    model: str | None = None,
    depth: int = 1,
    pid: int | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    log: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO spawns(tag, parent_session, role, task, model, depth, pid, "
        "worktree, branch, log, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tag, parent_session, role, task, model, depth, pid, worktree, branch,
         log, _now()),
    )
    conn.commit()


def set_spawn_pid(conn: sqlite3.Connection, tag: str, pid: int) -> None:
    conn.execute("UPDATE spawns SET pid=? WHERE tag=?", (pid, tag))
    conn.commit()


def mark_spawn_dead(conn: sqlite3.Connection, tag: str) -> None:
    conn.execute(
        "UPDATE spawns SET status='dead', ended_at=? "
        "WHERE tag=? AND status IN ('launched','registered')",
        (_now(), tag),
    )
    conn.commit()


def spawn_depth_of(conn: sqlite3.Connection, session_id: str | None) -> int:
    """How deep in the spawn tree this session sits. 0 for anything that was
    not spawned (a human terminal session, the daemon, @jorge). Read from the
    ledger, never from a caller's claim: an agent must not be able to present
    itself shallower than it is."""
    if not session_id:
        return 0
    row = conn.execute(
        "SELECT depth FROM spawns WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return int(row["depth"]) if row else 0


def live_spawns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows that claim to be running. Claim only — callers who need the truth
    must check the PID (see actions._reconcile_live)."""
    return conn.execute(
        "SELECT * FROM spawns WHERE status IN ('launched','registered') "
        "ORDER BY created_at"
    ).fetchall()


def list_spawns(conn: sqlite3.Connection, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM spawns ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["parent_display"] = display_name(
            get_session(conn, r["parent_session"]), r["parent_session"]
        )
        d["agent_display"] = (
            display_name(get_session(conn, r["session_id"]), r["session_id"])
            if r["session_id"] else None
        )
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Intents (the control plane)
# --------------------------------------------------------------------------- #
def request_intent(
    conn: sqlite3.Connection,
    *,
    from_session: str,
    from_handle: str | None,
    project: str | None,
    action: str,
    params: dict[str, Any],
) -> int:
    cur = conn.execute(
        "INSERT INTO intents(created_at, from_session, from_handle, project, action, "
        "params) VALUES (?,?,?,?,?,?)",
        (_now(), from_session, from_handle, project, action, json.dumps(params)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def take_next_intent(conn: sqlite3.Connection, worker: str) -> dict[str, Any] | None:
    """Atomically claim the oldest pending intent.

    One UPDATE..RETURNING rather than SELECT-then-UPDATE: the read and the write
    are a single statement, so two daemons can never hand the same row to two
    workers. Landing being serialised is the entire point of this table.
    """
    row = conn.execute(
        "UPDATE intents SET status='running', worker=?, started_at=?, attempts=attempts+1 "
        "WHERE id = (SELECT id FROM intents WHERE status='pending' ORDER BY id LIMIT 1) "
        "RETURNING *",
        (worker, _now()),
    ).fetchone()
    conn.commit()
    return intent_dict(row)


def finish_intent(
    conn: sqlite3.Connection,
    intent_id: int,
    *,
    status: str,
    result: dict[str, Any],
) -> None:
    if status not in ("done", "failed", "rejected"):
        raise ValueError(f"bad terminal status {status!r}")
    conn.execute(
        "UPDATE intents SET status=?, finished_at=?, result=? WHERE id=?",
        (status, _now(), json.dumps(result), intent_id),
    )
    conn.commit()


def intent_dict(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["params"] = json.loads(d["params"]) if d["params"] else {}
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def list_intents(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    q = "SELECT * FROM intents"
    a: list[Any] = []
    if status:
        q += " WHERE status=?"
        a.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    a.append(limit)
    out = [intent_dict(r) for r in conn.execute(q, a)]
    return [d for d in out if d is not None]


def get_intent(conn: sqlite3.Connection, intent_id: int) -> dict[str, Any] | None:
    return intent_dict(
        conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
    )


def reap_stale_intents(conn: sqlite3.Connection, older_than: float) -> int:
    """A daemon killed mid-action leaves rows stuck in 'running' forever. Fail
    them loudly rather than leaving a queue that silently never drains."""
    cutoff = _now() - older_than
    cur = conn.execute(
        "UPDATE intents SET status='failed', finished_at=?, "
        "result=json_object('error', 'daemon died while running this intent') "
        "WHERE status='running' AND started_at < ?",
        (_now(), cutoff),
    )
    conn.commit()
    return cur.rowcount


def list_claims(
    conn: sqlite3.Connection, *, project: str | None = None
) -> list[dict[str, Any]]:
    now = _now()
    q = "SELECT * FROM claims WHERE expires_at>?"
    args: list[Any] = [now]
    if project:
        q += " AND project=?"
        args.append(project)
    q += " ORDER BY acquired_at"
    out = []
    for r in conn.execute(q, args):
        d = dict(r)
        d["holder"] = display_name(get_session(conn, r["holder_session"]),
                                   r["holder_session"])
        d["expires_in"] = round(r["expires_at"] - now, 1)
        out.append(d)
    return out
