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
import socket
import sqlite3
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import names


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
    -- claude_pid is the Claude-specific rendezvous key: the owning `claude`
    -- process the SessionStart hook and the MCP server both descend from. It
    -- stays for the Claude PID walk and for memory attribution (procs.py). For
    -- the generic, client-independent lifecycle use owner_pid below.
    claude_pid    INTEGER,
    -- Which agent runtime owns this session: 'claude', 'codex', ... Lets one
    -- local bus hold sessions from different clients without confusing their
    -- identities. Defaults to 'claude' (the only client whose ancestry the PID
    -- rendezvous recognizes unaided; every pre-existing row was a Claude row).
    client        TEXT NOT NULL DEFAULT 'claude',
    -- The OS pid whose death retires this session, independent of client. For
    -- claude it mirrors claude_pid; for other clients it is the client process
    -- the MCP server is a child of. reap_dead_sessions keys on this. NULL for
    -- pseudo-sessions with no owning process (the human, the daemon).
    owner_pid     INTEGER,
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
-- idx_sessions_tag and idx_sessions_owner_pid are created in _migrate(), not
-- here: on an existing database the CREATE TABLE above is a no-op, so indexing
-- a freshly-added column from this script throws "no such column" before the
-- migration has a chance to add it.

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
    urgent        INTEGER NOT NULL DEFAULT 0,
    -- 'local'  => born here; the cloud mirror pushes it up.
    -- 'cloud'  => composed in the hosted dashboard and pulled down by the
    --             runner. The uplink must skip these or it echoes them back up
    --             as duplicates, forever.
    -- 'lan'    => pulled from a peer over the LAN (see lan.py). Like 'cloud' it
    --             is skipped by the uplink, so a LAN peer's messages never
    --             silently escape to the hosted mirror.
    origin        TEXT NOT NULL DEFAULT 'local',
    -- A globally-unique id minted at the row's birth, stable across machines.
    -- The autoincrement `id` is only meaningful on the machine that generated
    -- it; `guid` is what lets two peers (or the cloud downlink) dedup the same
    -- message instead of re-inserting it, so a message that hops A->B->C merges
    -- once. Unique index lives in _migrate (added to existing rows there too).
    guid          TEXT
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
    status        TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|rejected|expired
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

-- Cost telemetry: one row per hook fire. `injected_chars` is the size of the
-- additionalContext the hook pushed into the model's context (0 when silent);
-- `elapsed_ms` is the hook subprocess wall time. Together these answer "what
-- does running cabildo cost me" for the passive overhead: context tokens
-- (chars/~4) and per-tool-call latency. Written best-effort by the hook
-- dispatcher; a failure here never breaks a session.
CREATE TABLE IF NOT EXISTS hook_metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             REAL NOT NULL,
    session_id     TEXT,
    event          TEXT NOT NULL,
    injected_chars INTEGER NOT NULL DEFAULT 0,
    elapsed_ms     REAL
);
CREATE INDEX IF NOT EXISTS idx_hook_metrics_at ON hook_metrics(at);
CREATE INDEX IF NOT EXISTS idx_hook_metrics_session ON hook_metrics(session_id);

-- Machine-wide conditions: "RAM is at 4%", "these processes are orphaned", "CI
-- is down, nobody push". Deliberately NOT messages. A message is an event — it
-- happened, and it stays happened. A condition is a STATE, and three things
-- follow from that which the messages table cannot do:
--
--   * it reaches sessions that did not exist when it was raised. Channel unread
--     starts at join, on purpose (a fresh agent must not inherit a backlog), so
--     a message about memory pressure is invisible to exactly the session most
--     likely to make it worse — the one about to start.
--   * it is announced once per episode, not once per tool call, no matter how
--     long it lasts.
--   * it can be retracted. Injecting "RAM CRITICAL" into an agent an hour after
--     the machine recovered is worse than having said nothing.
--
-- Keyed by (host, key): the bus is a shared filesystem, and one laptop's memory
-- pressure is not news on another.
CREATE TABLE IF NOT EXISTS conditions (
    host        TEXT NOT NULL,
    key         TEXT NOT NULL,          -- 'mem' | 'orphans' | whatever a producer names
    state       TEXT NOT NULL DEFAULT 'raised',   -- raised | cleared
    level       TEXT NOT NULL,          -- info | warn | critical
    headline    TEXT NOT NULL,          -- one line, injected verbatim
    advice      TEXT,                   -- what an agent should DO about it
    -- JSON payload. A `by_session` map in here is rendered per-agent, which is
    -- what turns ambient anxiety into "YOU are holding 8 GB": the producer does
    -- the /proc work once for the whole fleet, and each hook pays one lookup.
    data        TEXT,
    source      TEXT,                   -- who raised it: daemon:<host>, @jorge
    -- Bumped whenever the news changes (raised, escalated, de-escalated,
    -- cleared) and never otherwise. Delivery is keyed off it: you hear each
    -- episode once, and a condition that merely persists stays silent.
    episode     INTEGER NOT NULL DEFAULT 1,
    raised_at   REAL NOT NULL,          -- onset of the current episode
    updated_at  REAL NOT NULL,
    -- A producer that dies must not leave an alarm ringing forever, so a
    -- condition fades unless something keeps refreshing it. Expiry is silent:
    -- we stopped looking, which is not the same as the machine recovering, and
    -- we will not tell agents it recovered.
    expires_at  REAL NOT NULL,
    PRIMARY KEY (host, key)
);

CREATE TABLE IF NOT EXISTS condition_deliveries (
    host          TEXT NOT NULL,
    key           TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    episode       INTEGER NOT NULL,
    delivered_at  REAL NOT NULL,
    PRIMARY KEY (host, key, session_id)
);
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
    if "client" not in have:
        # NOT NULL DEFAULT backfills every existing row to 'claude' in place:
        # before this migration the bus only ever held Claude sessions.
        conn.execute("ALTER TABLE sessions ADD COLUMN client TEXT NOT NULL DEFAULT 'claude'")
    if "owner_pid" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN owner_pid INTEGER")
        # One-time backfill, right after the column appears: every pre-existing
        # session's lifecycle was owned by its claude_pid, so the generic
        # owner_pid path sees exactly the identity the old claude_pid path saw.
        # Guarded by the "not in have" check so it runs once, not on every open;
        # a fresh DB has no rows and this is a harmless no-op there.
        conn.execute("UPDATE sessions SET owner_pid=claude_pid WHERE owner_pid IS NULL")
    # Unconditional: the columns now exist either way (fresh via SCHEMA, old via
    # the ALTERs above), and this is the only place it is safe to index them.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tag ON sessions(tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_pid ON sessions(owner_pid)")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "origin" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN origin TEXT NOT NULL DEFAULT 'local'")
    if "guid" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN guid TEXT")
        # Backfill every pre-existing row with a stable guid so cross-machine
        # dedup has something to key on for history, not just new messages. One
        # time, guarded by "not in have"; a fresh DB has no rows and skips it.
        for r in conn.execute("SELECT id FROM messages WHERE guid IS NULL").fetchall():
            conn.execute("UPDATE messages SET guid=? WHERE id=?", (uuid.uuid4().hex, r["id"]))
    # Unconditional (fresh via SCHEMA, old via the ALTER above): the only safe
    # place to index the column. UNIQUE is what makes post_message idempotent by
    # guid; SQLite treats NULLs as distinct, so it tolerates any un-backfilled row.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_guid ON messages(guid)")
    conn.commit()


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def ensure_topic(
    conn: sqlite3.Connection, name: str, project: str | None = None, created_by: str | None = None
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO topics(name, project, created_by, created_at) VALUES (?,?,?,?)",
        (name, project, created_by, _now()),
    )


def subscribe(conn: sqlite3.Connection, session_id: str, topic: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions(session_id, topic, joined_at) VALUES (?,?,?)",
        (session_id, topic, _now()),
    )


def unsubscribe(conn: sqlite3.Connection, session_id: str, topic: str) -> None:
    conn.execute("DELETE FROM subscriptions WHERE session_id=? AND topic=?", (session_id, topic))


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
    client: str | None = None,
    owner_pid: int | None = None,
) -> None:
    """Idempotent upsert of a session + its default subscriptions. Called from
    the SessionStart hook (authoritative session_id) and, defensively, from the
    MCP server. Only overwrites columns we were actually given.

    `client` names the owning agent runtime ('claude', 'codex', ...) and
    `owner_pid` the OS pid whose death retires the session. When `owner_pid` is
    not given it mirrors `claude_pid`, so existing Claude callers (the hooks)
    get the generic lifecycle for free without passing anything new. `client`
    left None preserves an existing row's value (a re-register never downgrades
    a 'codex' row to 'claude') and defaults to 'claude' only on first insert.
    """
    now = _now()
    if owner_pid is None:
        owner_pid = claude_pid
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sessions(session_id, cwd, project, project_name, claude_pid, "
            "tag, client, owner_pid, status, created_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?)",
            (
                session_id,
                cwd,
                project,
                project_name,
                claude_pid,
                tag,
                client or "claude",
                owner_pid,
                now,
                now,
            ),
        )
    else:
        conn.execute(
            "UPDATE sessions SET "
            "cwd=COALESCE(?,cwd), project=COALESCE(?,project), "
            "project_name=COALESCE(?,project_name), claude_pid=COALESCE(?,claude_pid), "
            "tag=COALESCE(?,tag), client=COALESCE(?,client), owner_pid=COALESCE(?,owner_pid), "
            "status='active', last_seen_at=? WHERE session_id=?",
            (cwd, project, project_name, claude_pid, tag, client, owner_pid, now, session_id),
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
    return conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()


def session_for_tag(conn: sqlite3.Connection, tag: str) -> str | None:
    """Resolve identity from the env tag a spawned agent was born with. Exact,
    unlike the PID walk: a spawned `claude -p` is not a terminal's descendant,
    and the ancestor search happily finds somebody else's claude.
    """
    if not tag:
        return None
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE tag=? ORDER BY last_seen_at DESC LIMIT 1",
        (tag,),
    ).fetchone()
    return row["session_id"] if row else None


def session_for_pid(conn: sqlite3.Connection, claude_pid: int) -> str | None:
    """Reverse the rendezvous: given the resolved claude PID, return the
    session_id the SessionStart hook bound to it (most-recently-seen wins).
    """
    if not claude_pid:
        return None
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE claude_pid=? ORDER BY last_seen_at DESC LIMIT 1",
        (claude_pid,),
    ).fetchone()
    return row["session_id"] if row else None


def session_for_owner(conn: sqlite3.Connection, owner_pid: int, client: str) -> str | None:
    """Client-independent rendezvous: the most-recently-seen active session of
    `client` owned by `owner_pid`.

    Weaker than the Claude PID walk or an explicit CABILDO_SESSION_ID: a host
    process shared by several sessions (multiple Codex threads in one process)
    is ambiguous by owner pid alone, so this returns the freshest one and is
    only used as a last resort before minting a provisional id. Scoped to a
    single `client` so a reused pid can never fuse a Claude and a Codex session.
    """
    if not owner_pid:
        return None
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE owner_pid=? AND client=? AND status='active' "
        "ORDER BY last_seen_at DESC LIMIT 1",
        (owner_pid, client),
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
    """Human label for a session: @handle if set, else its robot codename.

    The codename (BENDER-8F) is a deterministic function of the session_id, so
    it doubles as the id — and it's what replaced the old bland session_id[:8].
    """
    if row is not None:
        if row["handle"]:
            return f"@{row['handle']}"
        session_id = row["session_id"]
    return names.robot_name(session_id)


def session_for_robot_name(conn: sqlite3.Connection, name: str) -> str | None:
    """Reverse a robot codename (BENDER-8F) back to its session_id.

    Deterministic names mean no lookup table: recompute the name over active
    sessions and match case-insensitively. Most-recently-seen wins on the
    (rare) collision, mirroring the short-id resolver.
    """
    want = name.lstrip("@").upper()
    for r in conn.execute(
        "SELECT session_id FROM sessions WHERE status='active' ORDER BY last_seen_at DESC"
    ):
        if names.robot_name(r["session_id"]).upper() == want:
            return r["session_id"]
    return None


def subscriptions_of(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return [
        r["topic"]
        for r in conn.execute("SELECT topic FROM subscriptions WHERE session_id=?", (session_id,))
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
    origin: str = "local",
    guid: str | None = None,
    created_at: float | None = None,
) -> int:
    """Insert a message and return its local id.

    `guid` is the global identity; a fresh one is minted for a locally-born
    message, but the cloud downlink and the LAN transport pass the original so
    the same message is not re-inserted on another machine. Insert is idempotent
    by guid (ON CONFLICT DO NOTHING) — a second post of the same guid returns the
    existing row's id instead of a duplicate. `created_at` is likewise preserved
    across a hop so ordering and merge stay consistent everywhere.
    """
    if (topic is None) == (to_session is None):
        raise ValueError("exactly one of topic / to_session must be set")
    guid = guid or uuid.uuid4().hex
    ts = _now() if created_at is None else created_at
    cur = conn.execute(
        "INSERT INTO messages(created_at, from_session, from_handle, topic, to_session, "
        "project, body, urgent, origin, guid) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(guid) DO NOTHING",
        (
            ts,
            from_session,
            from_handle,
            topic,
            to_session,
            project,
            body,
            1 if urgent else 0,
            origin,
            guid,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:  # guid already present — return the existing row's id
        row = conn.execute("SELECT id FROM messages WHERE guid=?", (guid,)).fetchone()
        return int(row["id"]) if row else 0
    return int(cur.lastrowid or 0)


def _inbox_where(session_id: str) -> tuple[str, list[Any]]:
    """SQL predicate for 'messages addressed to me': DMs to me, or channel
    messages posted to a topic I'm subscribed to *after I joined it*. Never my
    own messages.

    The joined_at bound is what keeps a fresh session from inheriting a
    channel's entire backlog as unread: subscribing to #proj:paas with 100
    prior messages must not hand the new agent 100 unread (~50k tokens of
    context) the moment it starts. A DM is always yours regardless of timing.
    """
    return (
        "(m.to_session=? OR EXISTS (SELECT 1 FROM subscriptions s "
        "WHERE s.session_id=? AND s.topic=m.topic AND m.created_at >= s.joined_at)) "
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


def inbox_count(conn: sqlite3.Connection, session_id: str, *, unread_only: bool = True) -> int:
    """How many messages the inbox would return with no limit. Lets a capped
    `inbox` say how much it is holding back instead of just "there is more".
    """
    where, args = _inbox_where(session_id)
    q = (
        "SELECT COUNT(*) AS n FROM messages m "
        "LEFT JOIN reads r ON r.message_id=m.id AND r.session_id=? "
        f"WHERE {where}"
    )
    if unread_only:
        q += " AND r.read_at IS NULL"
    return int(conn.execute(q, [session_id, *args]).fetchone()["n"])


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


def get_message(
    conn: sqlite3.Connection, session_id: str, message_id: int
) -> dict[str, Any] | None:
    """Full record of a single message, but only if it is actually in this
    session's inbox. This is the companion to the previews `inbox` returns: the
    agent reads compact previews, then fetches the full body of the one it cares
    about by id — instead of every full body flooding context up front.
    """
    where, args = _inbox_where(session_id)
    row = conn.execute(
        "SELECT m.*, r.read_at AS read_at FROM messages m "
        "LEFT JOIN reads r ON r.message_id=m.id AND r.session_id=? "
        f"WHERE {where} AND m.id=?",
        [session_id, *args, message_id],
    ).fetchone()
    return _msg_dict(row) if row else None


def mark_read(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    message_ids: Iterable[int] | None = None,
    topic: str | None = None,
    all_unread: bool = False,
) -> int:
    """Explicitly mark messages read. Returns number newly marked. Only ever
    marks messages that are actually in this session's inbox.
    """
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
    PostToolUse hook injects these once and records them in `deliveries`.
    """
    where, args = _inbox_where(session_id)
    q = (
        "SELECT m.*, r.read_at AS read_at FROM messages m "
        "LEFT JOIN reads r ON r.message_id=m.id AND r.session_id=? "
        "LEFT JOIN deliveries d ON d.message_id=m.id AND d.session_id=? "
        f"WHERE {where} AND m.urgent=1 AND r.read_at IS NULL "
        "AND d.delivered_at IS NULL ORDER BY m.created_at ASC LIMIT ?"
    )
    return [_msg_dict(r) for r in conn.execute(q, [session_id, session_id, *args, limit])]


def mark_delivered(conn: sqlite3.Connection, session_id: str, message_ids: Iterable[int]) -> None:
    now = _now()
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries(message_id, session_id, delivered_at) VALUES (?,?,?)",
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
    pair of session_ids for a DM thread.
    """
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
    d["from_display"] = (
        f"@{r['from_handle']}" if r["from_handle"] else names.robot_name(r["from_session"])
    )
    d["read"] = ("read_at" in d) and d.get("read_at") is not None
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
    or already held by us (in which case the lease is refreshed).
    """
    now = _now()
    row = conn.execute("SELECT * FROM claims WHERE resource=?", (resource,)).fetchone()
    if row and row["expires_at"] > now and row["holder_session"] != holder_session:
        return {
            "ok": False,
            "resource": resource,
            "held_by": display_name(
                get_session(conn, row["holder_session"]), row["holder_session"]
            ),
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
        (tag, parent_session, role, task, model, depth, pid, worktree, branch, log, _now()),
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
    itself shallower than it is.
    """
    if not session_id:
        return 0
    row = conn.execute(
        "SELECT depth FROM spawns WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return int(row["depth"]) if row else 0


def live_spawns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows that claim to be running. Claim only — callers who need the truth
    must check the PID (see actions._reconcile_live).
    """
    return conn.execute(
        "SELECT * FROM spawns WHERE status IN ('launched','registered') ORDER BY created_at"
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
            if r["session_id"]
            else None
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
    # 'expired' is its own terminal state, not a flavour of 'failed': nothing was
    # attempted, so nothing failed, and a retry-on-failure reader must be able to
    # tell those apart.
    if status not in ("done", "failed", "rejected", "expired"):
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
    return intent_dict(conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone())


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a live process. PermissionError means it exists but
    isn't ours (still alive); ProcessLookupError / bad pid means gone.
    """
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reap_dead_sessions(conn: sqlite3.Connection, is_alive=None) -> int:
    """Mark 'active' sessions ended when their owning process is gone.

    The SessionEnd hook is the clean path, but a killed terminal or crashed
    client never fires it, leaving zombie 'active' rows that accumulate unread
    forever and clutter list_agents (74 rows, ~48 'active', ~5 truly running as
    of this writing). Liveness keys on owner_pid — the client-independent owning
    process — falling back to claude_pid for any pre-migration row not yet
    rewritten (owner_pid was backfilled from claude_pid, so the two agree for
    Claude). A session whose owning pid no longer resolves to a live process is
    ended via end_session (which also releases its claims and closes its spawn
    rows). Under-reaps by design: a recycled PID that now belongs to some other
    live process just isn't reaped, which is the safe direction. Sessions with
    no recorded pid (the human, the daemon) are left to last_seen aging.
    """
    alive = is_alive or _pid_alive
    rows = conn.execute(
        "SELECT session_id, owner_pid, claude_pid FROM sessions WHERE status='active'"
    ).fetchall()
    reaped = 0
    for r in rows:
        pid = r["owner_pid"] or r["claude_pid"]
        if pid and not alive(pid):
            end_session(conn, r["session_id"])
            reaped += 1
    return reaped


def record_hook_metric(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    event: str,
    injected_chars: int,
    elapsed_ms: float | None,
) -> None:
    """Append one cost-telemetry row for a hook fire. Best-effort: callers wrap
    this so a metrics write never propagates an error into the user's session.
    """
    conn.execute(
        "INSERT INTO hook_metrics(at, session_id, event, injected_chars, elapsed_ms) "
        "VALUES (?,?,?,?,?)",
        (_now(), session_id, event, int(injected_chars), elapsed_ms),
    )
    conn.commit()


def hook_metrics_summary(
    conn: sqlite3.Connection,
    *,
    since: float | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate hook cost telemetry, optionally since a timestamp / for one
    session. Returns overall totals plus a per-event breakdown.
    """
    where = "1=1"
    args: list[Any] = []
    if since is not None:
        where += " AND at >= ?"
        args.append(since)
    if session_id is not None:
        where += " AND session_id = ?"
        args.append(session_id)

    per_event = [
        dict(r)
        for r in conn.execute(
            f"SELECT event, COUNT(*) AS fires, "
            f"COALESCE(SUM(injected_chars),0) AS chars, "
            f"COALESCE(SUM(CASE WHEN injected_chars>0 THEN 1 ELSE 0 END),0) AS injections, "
            f"AVG(elapsed_ms) AS avg_ms, COALESCE(SUM(elapsed_ms),0) AS total_ms "
            f"FROM hook_metrics WHERE {where} GROUP BY event ORDER BY chars DESC",
            args,
        )
    ]
    total = conn.execute(
        f"SELECT COUNT(*) AS fires, COALESCE(SUM(injected_chars),0) AS chars, "
        f"COALESCE(SUM(elapsed_ms),0) AS total_ms, "
        f"COUNT(DISTINCT session_id) AS sessions, MIN(at) AS first_at, MAX(at) AS last_at "
        f"FROM hook_metrics WHERE {where}",
        args,
    ).fetchone()
    return {"total": dict(total), "per_event": per_event}


def count_pending_intents(conn: sqlite3.Connection, *, older_than: float = 0.0) -> int:
    """How many intents are waiting — all of them, or only those queued more than
    `older_than` seconds ago. The daemon says this out loud at startup: a backlog
    it is about to refuse is worth one line, and a backlog it is about to RUN is
    worth even more.
    """
    q = "SELECT COUNT(*) AS n FROM intents WHERE status='pending'"
    a: list[Any] = []
    if older_than:
        q += " AND created_at < ?"
        a.append(_now() - older_than)
    return int(conn.execute(q, a).fetchone()["n"])


def reap_stale_intents(conn: sqlite3.Connection, older_than: float) -> int:
    """A daemon killed mid-action leaves rows stuck in 'running' forever. Fail
    them loudly rather than leaving a queue that silently never drains.
    """
    cutoff = _now() - older_than
    cur = conn.execute(
        "UPDATE intents SET status='failed', finished_at=?, "
        "result=json_object('error', 'daemon died while running this intent') "
        "WHERE status='running' AND started_at < ?",
        (_now(), cutoff),
    )
    conn.commit()
    return cur.rowcount


def list_claims(conn: sqlite3.Connection, *, project: str | None = None) -> list[dict[str, Any]]:
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
        d["holder"] = display_name(get_session(conn, r["holder_session"]), r["holder_session"])
        d["expires_in"] = round(r["expires_at"] - now, 1)
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Conditions — machine-wide state every session on the host should know about
# --------------------------------------------------------------------------- #
CONDITION_LEVELS = ("info", "warn", "critical")
# How long a raise stands without a refresh. Producers re-raise on their sweep;
# four sweeps of silence is a dead producer, not a persistent condition.
CONDITION_TTL = 300.0
# How long a cleared condition lingers so the all-clear can reach the sessions
# that heard the raise. They only collect it on their next tool call, and an
# agent can easily spend half an hour on one.
CLEARED_GRACE = 1800.0
# An expired row refreshed at the same level is the same episode continuing —
# unless it has been dark so long that we plainly stopped watching, in which
# case what we know about the interval is nothing, and the honest move is to
# announce it again.
_LOST_AFTER = 60.0


def hostname() -> str:
    return socket.gethostname() or "localhost"


def _level_rank(level: str) -> int:
    try:
        return CONDITION_LEVELS.index(level)
    except ValueError:
        return 0


def _condition_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    try:
        d["data"] = json.loads(r["data"]) if r["data"] else {}
    except (TypeError, ValueError):
        d["data"] = {}
    d["age"] = round(_now() - r["raised_at"], 1)
    return d


def raise_condition(
    conn: sqlite3.Connection,
    *,
    key: str,
    level: str = "warn",
    headline: str,
    advice: str | None = None,
    data: dict[str, Any] | None = None,
    source: str | None = None,
    host: str | None = None,
    ttl_seconds: float = CONDITION_TTL,
) -> dict[str, Any]:
    """Raise or refresh a condition. Returns the row, with `announced` telling
    the caller whether this is news (a new episode) or a heartbeat.

    Refreshing an unchanged condition is deliberately silent — a producer
    sampling every 30 seconds must not re-announce "RAM is low" 120 times an
    hour. The level *changing* is the news, in either direction: an agent told
    to stop starting builds is owed the update when it drops back to a warning.
    """
    host = host or hostname()
    now = _now()
    row = conn.execute("SELECT * FROM conditions WHERE host=? AND key=?", (host, key)).fetchone()

    lost = bool(row) and row["expires_at"] < now - _LOST_AFTER
    fresh = row is None or row["state"] != "raised" or lost
    announced = fresh or (row is not None and row["level"] != level)
    episode = (row["episode"] + 1) if (row and announced) else (row["episode"] if row else 1)
    # `not fresh` already implies row is not None (fresh includes `row is None`), but the
    # type checker can't narrow through the intermediate bool — guard row explicitly.
    raised_at = row["raised_at"] if (row is not None and not fresh) else now

    conn.execute(
        "INSERT INTO conditions(host, key, state, level, headline, advice, data, "
        "source, episode, raised_at, updated_at, expires_at) "
        "VALUES (?,?,'raised',?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(host, key) DO UPDATE SET "
        "state='raised', level=excluded.level, headline=excluded.headline, "
        "advice=excluded.advice, data=excluded.data, source=excluded.source, "
        "episode=excluded.episode, raised_at=excluded.raised_at, "
        "updated_at=excluded.updated_at, expires_at=excluded.expires_at",
        (
            host,
            key,
            level,
            headline,
            advice,
            json.dumps(data) if data else None,
            source,
            episode,
            raised_at,
            now,
            now + ttl_seconds,
        ),
    )
    conn.commit()
    out = _condition_dict(
        conn.execute("SELECT * FROM conditions WHERE host=? AND key=?", (host, key)).fetchone()
    )
    out["announced"] = announced
    return out


def clear_condition(
    conn: sqlite3.Connection,
    *,
    key: str,
    headline: str | None = None,
    host: str | None = None,
    source: str | None = None,
) -> bool:
    """Retract a condition. False if it wasn't raised.

    The row is kept, cleared, for CLEARED_GRACE — that is what lets the all-clear
    be delivered. Only sessions that were told about the raise get told about the
    recovery; announcing good news about a problem you never heard of is noise.
    """
    host = host or hostname()
    now = _now()
    row = conn.execute(
        "SELECT * FROM conditions WHERE host=? AND key=? AND state='raised'",
        (host, key),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE conditions SET state='cleared', level='info', headline=?, "
        "advice=NULL, data=NULL, source=COALESCE(?, source), episode=episode+1, "
        "updated_at=?, expires_at=? WHERE host=? AND key=?",
        (headline or f"{key}: recovered", source, now, now + CLEARED_GRACE, host, key),
    )
    conn.commit()
    return True


def active_conditions(
    conn: sqlite3.Connection, *, host: str | None = None, include_cleared: bool = False
) -> list[dict[str, Any]]:
    """Conditions currently in force on this host, worst first."""
    host = host or hostname()
    q = "SELECT * FROM conditions WHERE host=? AND expires_at>?"
    if not include_cleared:
        q += " AND state='raised'"
    rows = [_condition_dict(r) for r in conn.execute(q, (host, _now()))]
    rows.sort(key=lambda d: (-_level_rank(d["level"]), d["raised_at"]))
    return rows


def undelivered_conditions(
    conn: sqlite3.Connection, session_id: str, *, host: str | None = None
) -> list[dict[str, Any]]:
    """Condition episodes this session has not been shown yet.

    The `d.episode IS NOT NULL` guard on cleared rows is what keeps a fresh
    session from being handed the tail end of a story it never heard: you are
    only told a condition recovered if you were told it existed.
    """
    host = host or hostname()
    rows = conn.execute(
        "SELECT c.* FROM conditions c "
        "LEFT JOIN condition_deliveries d "
        "  ON d.host=c.host AND d.key=c.key AND d.session_id=? "
        "WHERE c.host=? AND c.expires_at>? "
        "  AND (d.episode IS NULL OR d.episode < c.episode) "
        "  AND (c.state='raised' OR d.episode IS NOT NULL)",
        (session_id, host, _now()),
    ).fetchall()
    out = [_condition_dict(r) for r in rows]
    out.sort(key=lambda d: (-_level_rank(d["level"]), d["raised_at"]))
    return out


def mark_conditions_delivered(
    conn: sqlite3.Connection, session_id: str, conditions: Iterable[dict[str, Any]]
) -> None:
    now = _now()
    conn.executemany(
        "INSERT INTO condition_deliveries(host, key, session_id, episode, delivered_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(host, key, session_id) DO UPDATE SET "
        "episode=excluded.episode, delivered_at=excluded.delivered_at",
        [(c["host"], c["key"], session_id, c["episode"], now) for c in conditions],
    )
    conn.commit()


def prune_conditions(conn: sqlite3.Connection) -> int:
    """Drop conditions past their expiry, and delivery receipts for conditions
    that no longer exist. Called on the producer's sweep.
    """
    cur = conn.execute("DELETE FROM conditions WHERE expires_at<?", (_now(),))
    conn.execute(
        "DELETE FROM condition_deliveries WHERE NOT EXISTS "
        "(SELECT 1 FROM conditions c WHERE c.host=condition_deliveries.host "
        " AND c.key=condition_deliveries.key)"
    )
    conn.commit()
    return cur.rowcount
