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
import subprocess
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
# Seen more recently than this = actively working (a tool call refreshes
# last_seen every few seconds via the hooks). Between this and ACTIVE_WINDOW is
# idle; past ACTIVE_WINDOW is stale and a reaper will retire it.
WORKING_WINDOW = 90

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
    -- The model this session is running (e.g. claude-opus-4-8), when known. It is
    -- an agent-card field: peers read it via list_agents to route by capability
    -- (ask the stronger model to review). Filled from the transcript during
    -- `record` ingest; NULL until a model is observed or for clients not recorded.
    model         TEXT,
    -- Agent-card capability label: the function this session is playing on the
    -- bus (e.g. 'reviewer', 'architect'). Like `model`, peers read it via
    -- list_agents to route by capability, but a role is *declared*, not observed:
    -- the model tells you how strong an agent is, the role tells you what it is
    -- for. May be one of the project's configured roster names (config `roles`)
    -- or a freehand string; either way the responsibilities live alongside it.
    role          TEXT,
    -- Free text describing what this role is accountable for. Defaults to the
    -- roster entry's text when `role` names a configured role and none is given.
    responsibilities TEXT,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | ended
    created_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    -- Last time the wall-clock timeline line was injected into this session (see
    -- notify.timeline_text). A throttle, not presence: hooks have no timer, so
    -- the line rides the next hook event and this stamp is what keeps it to one
    -- per interval rather than one per tool call.
    time_injected_at REAL,
    -- Last time the Stop hook fired the one-shot heartbeat nudge for this session
    -- (see notify.heartbeat_reason). Separate from time_injected_at so the
    -- mid-turn timeline line and the at-idle nudge keep independent cadences.
    heartbeat_at REAL
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
    created_at  REAL NOT NULL,
    -- 1 => a private room: a listen/send grant is required to join or post.
    -- 0 (default) => open-join, today's #general model. A proj:<name> channel
    -- stays gated by project identity, not by this flag.
    gated       INTEGER NOT NULL DEFAULT 0
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
    -- 'local'  => born on this machine.
    -- 'lan'    => pulled from a peer over the LAN (see lan.py).
    -- 'wan'    => pulled from an explicit remote peer over a tunnel (see wan.py).
    provenance    TEXT NOT NULL DEFAULT 'local',
    -- A globally-unique id minted at the row's birth, stable across machines.
    -- The autoincrement `id` is only meaningful on the machine that generated
    -- it; `guid` is what lets two peers dedup the same message instead of
    -- re-inserting it, so a message that hops A->B->C merges once. Unique index
    -- lives in _migrate (added to existing rows there too).
    guid          TEXT,
    -- When THIS machine stored the row. created_at is the sender's clock, which a
    -- paired machine may run seconds apart; deliver-after-join compares this one.
    received_at   REAL
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
    result        TEXT,                            -- JSON object
    lane          TEXT                             -- intents of one lane run one at a time
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

-- Conversation turns, distilled from each client's transcript into cabildo's own
-- store so minutes/search/analytics work off one DB instead of the per-client
-- transcript files. Opt-in only: the daemon writes here solely when started with
-- --save-messages AND the turn's project has [record] messages = true. `uuid` is
-- the transcript entry's own id, so re-ingesting a file (rotation, overlap) is an
-- INSERT OR IGNORE no-op. Only user/assistant text is stored; tool traffic and
-- hook/meta lines are dropped. These rows stay on the machine that recorded them.
CREATE TABLE IF NOT EXISTS transcript_messages (
    session_id    TEXT NOT NULL,
    uuid          TEXT NOT NULL,
    ts            REAL,
    role          TEXT NOT NULL,                     -- user | assistant
    model         TEXT,                              -- assistant model id; NULL for user
    client        TEXT NOT NULL DEFAULT 'claude',
    project       TEXT,
    project_name  TEXT,
    body          TEXT NOT NULL,
    PRIMARY KEY (session_id, uuid)
);
CREATE INDEX IF NOT EXISTS idx_tmsg_project ON transcript_messages(project_name, ts);
CREATE INDEX IF NOT EXISTS idx_tmsg_session ON transcript_messages(session_id, ts);

-- Per-session byte offset into the transcript file, so each ingest pass reads
-- only the new tail. Restart-safe: transcripts are append-only, so the offset is
-- a stable cursor (transcript.session_messages handles truncation/rotation).
CREATE TABLE IF NOT EXISTS ingest_cursors (
    session_id    TEXT PRIMARY KEY,
    byte_offset   INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);

-- Durable decisions, separate from chat. Chat scrolls away and dies with a
-- session's context; a decision (why we chose X, what we rejected, a constraint,
-- a warning) is the coordination memory that must outlive any one agent's
-- window, so a peer coming in cold can `read_decisions` instead of re-deriving it. Kept
-- deliberately apart from `messages` so the log of what-was-decided is not buried
-- in the log of what-was-said.
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,
    session_id    TEXT,
    handle        TEXT,
    project       TEXT,
    project_name  TEXT,
    kind          TEXT NOT NULL DEFAULT 'decision',  -- decision | fact | constraint | warning
    title         TEXT NOT NULL,
    body          TEXT,
    refs          TEXT,                              -- files/commit the decision touches
    tags          TEXT                               -- freeform, for filtering
);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_name, created_at);

-- Session notes (issue #34): loose ends scoped to the SESSION that raised them,
-- not the whole project. Jorge opens many topics in one session and some get
-- lost; an agent adds a note when a topic starts, and `/wrap-up` lists this
-- session's open notes and triages them (finish now / hand off / park) rather
-- than re-reading a long, maybe-summarised conversation. A note is mutable:
-- `state` moves open -> done (finished here) or open -> parked (promoted to a
-- project issue / agenda item, recorded in `parked_ref`), so a session can be
-- closed once all of its notes are done or parked.
--
--   type       : topic | need | bug | idea | unfinished
--   state      : open | done | parked
--   parked_ref : where a parked note went (issue file name, agenda item, MR…)
CREATE TABLE IF NOT EXISTS session_notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    session_id    TEXT,
    handle        TEXT,
    project       TEXT,
    project_name  TEXT,
    type          TEXT NOT NULL DEFAULT 'topic',
    text          TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'open',
    parked_ref    TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_notes_session ON session_notes(session_id, state, id);

-- Cross-project consent (issue #26). A grant is one DIRECTIONAL, SCOPED row:
-- project `grantee` may, with `capability`, reach `resource`. It is the unit of
-- deliberate interaction on an otherwise isolated bus — discovery (the
-- directory) never implies delivery, a grant does. A `cabildo project --link`
-- is the coarse mutual case (kept in cloud.json); these rows add the fine,
-- directional, per-resource cases a link cannot express, plus the
-- request->decide lifecycle.
--
--   grantee       : project name that receives access
--   resource_kind : 'project' | 'topic' | 'agent'
--   resource      : a project name, a topic name, or a session id
--   capability    : 'listen' (subscribe/read) | 'send' (post/DM)
--   status        : 'requested' | 'granted' | 'denied' | 'revoked'
--
-- One row per (grantee, resource_kind, resource, capability): a re-request or a
-- re-grant updates the same row rather than piling up history.
CREATE TABLE IF NOT EXISTS grants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    grantee       TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource      TEXT NOT NULL,
    capability    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'requested',
    requested_by  TEXT,                    -- session id that asked
    decided_by    TEXT,                    -- session id / human that decided
    note          TEXT,
    created_at    REAL NOT NULL,
    decided_at    REAL,
    UNIQUE (grantee, resource_kind, resource, capability)
);
CREATE INDEX IF NOT EXISTS idx_grants_grantee ON grants(grantee, status);
CREATE INDEX IF NOT EXISTS idx_grants_resource ON grants(resource_kind, resource, status);

-- The attention engine's schedule inputs: the lines the human sets for the
-- fleet to wind down against (the "I stop at 23:00" of the slot-machine issue).
-- Durable because the nudge is computed from this row at a session's next tool
-- call, not pushed by a producer, so a bare `cabildo stop 23:00` steers every
-- session even with no daemon up.
--
--   scope   : 'global' (the whole machine's fleet) | a project path (one repo)
--   kind    : 'stop' (a hard line for the day) | 'block' (a bounded break; later)
--   line_at : the wall-clock moment a session is steered TOWARD — the stop time,
--             or a block's start. ends_at bounds a block; NULL for a stop.
--   active  : 0 retires a line (superseded by a newer stop for the same scope,
--             or cleared by hand) without deleting the history.
CREATE TABLE IF NOT EXISTS schedule (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host        TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'global',
    kind        TEXT NOT NULL DEFAULT 'stop',
    label       TEXT,
    line_at     REAL NOT NULL,
    ends_at     REAL,
    created_by  TEXT,
    created_at  REAL NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_schedule_active ON schedule(host, active, line_at);

-- Per-session wind-down delivery receipts. `step` is the highest escalation
-- step this session has already been shown for this line (1=heads-up, 2=land-it,
-- 3=past-the-line); a session hears each step at most once, and a line that
-- merely persists in the same step stays silent — the same once-per-episode
-- discipline the conditions channel uses, keyed to the step instead.
CREATE TABLE IF NOT EXISTS schedule_deliveries (
    schedule_id   INTEGER NOT NULL,
    session_id    TEXT NOT NULL,
    step          INTEGER NOT NULL,
    delivered_at  REAL NOT NULL,
    PRIMARY KEY (schedule_id, session_id)
);
"""


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def connect(*, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the shared bus DB in WAL mode. ``check_same_thread=False`` is for the
    broker, which owns one connection and serialises access to it across its accept
    threads with its own lock; every other caller keeps the safe default.
    """
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to tables that already exist, and perform guarded renames.

    CREATE TABLE IF NOT EXISTS is a no-op against a live database, so a new
    column in SCHEMA silently never appears on anyone's actual bus — the schema
    would claim a shape the file doesn't have. Mostly additive; one guarded
    rename (facts→decisions, messages.origin→provenance) is included.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "tag" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN tag TEXT")
    if "client" not in have:
        # NOT NULL DEFAULT backfills every existing row to 'claude' in place:
        # before this migration the bus only ever held Claude sessions.
        conn.execute("ALTER TABLE sessions ADD COLUMN client TEXT NOT NULL DEFAULT 'claude'")
    if "model" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
    if "role" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN role TEXT")
    if "responsibilities" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN responsibilities TEXT")
    if "time_injected_at" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN time_injected_at REAL")
    if "heartbeat_at" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN heartbeat_at REAL")
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

    have = {r["name"] for r in conn.execute("PRAGMA table_info(intents)")}
    if "lane" not in have:
        conn.execute("ALTER TABLE intents ADD COLUMN lane TEXT")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "origin" in have and "provenance" not in have:
        conn.execute("ALTER TABLE messages RENAME COLUMN origin TO provenance")
    elif "provenance" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN provenance TEXT NOT NULL DEFAULT 'local'")
    # Re-fetch after potential rename so guid check sees the current columns.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "guid" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN guid TEXT")
        # Backfill every pre-existing row with a stable guid so cross-machine
        # dedup has something to key on for history, not just new messages. One
        # time, guarded by "not in have"; a fresh DB has no rows and skips it.
        for r in conn.execute("SELECT id FROM messages WHERE guid IS NULL").fetchall():
            conn.execute("UPDATE messages SET guid=? WHERE id=?", (uuid.uuid4().hex, r["id"]))
    if "received_at" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN received_at REAL")
    # Unconditional (fresh via SCHEMA, old via the ALTER above): the only safe
    # place to index the column. UNIQUE is what makes post_message idempotent by
    # guid; SQLite treats NULLs as distinct, so it tolerates any un-backfilled row.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_guid ON messages(guid)")

    # Rename the facts table to decisions (one-time, guarded).
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "facts" in tables and "decisions" not in tables:
        conn.execute("ALTER TABLE facts RENAME TO decisions")

    # topics.gated: new column on an existing table (issue #26). Default 0 keeps
    # every existing topic open-join; the grants layer gates only what is flagged.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(topics)")}
    if "gated" not in have:
        conn.execute("ALTER TABLE topics ADD COLUMN gated INTEGER NOT NULL DEFAULT 0")

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
        "SELECT session_id, project_name FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    old_pname = (row["project_name"] if row else None) or None
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
    # An agent's home is its OWN project channel (isolation by default) plus one
    # chatroom, #general, which every session joins: the project channel is for
    # the repo, the room is for the swarm, across projects and across paired
    # machines. [chat] room renames it; an empty room turns the auto-join off.
    ensure_topic(conn, DEFAULT_TOPIC)  # keep the room available to join
    _join_room(conn, session_id)
    # The effective name after this upsert: a caller that didn't pass one leaves
    # the stored value in place (COALESCE above), so fall back to the old name.
    new_pname = project_name or old_pname
    # Reconcile a project change: a reused session that moved to a new project
    # must stop delivering into its former proj: channel (issue #26, leak #3).
    if old_pname and new_pname and old_pname.strip().lower() != new_pname.strip().lower():
        unsubscribe(conn, session_id, project_topic(old_pname))
    if new_pname:
        # A catch-all name (self/tmp/a home dir) marked `[project.<name>].isolated`
        # gets NO shared room — agents there are DM-only (issue #26, leak #2).
        try:
            from . import config

            isolated = config.project_isolated(cwd, new_pname)
        except Exception:  # config is advisory; its failure must not block a session
            isolated = False
        pt = project_topic(new_pname)
        if isolated:
            unsubscribe(conn, session_id, pt)
        else:
            ensure_topic(conn, pt, project=project, created_by=session_id)
            subscribe(conn, session_id, pt)
    conn.commit()


def _join_room(conn: sqlite3.Connection, session_id: str) -> None:
    """Put a session in the chatroom. Best-effort: it never blocks a session starting.
    Isolated projects join too: a catch-all folder like a home directory is exactly
    where the room is the only way to be reached.
    """
    try:
        from . import config

        name = config.chat_room()
        if name:
            ensure_topic(conn, name, created_by=session_id)
            subscribe(conn, session_id, name)
    except Exception:
        pass


def join_room_all(conn: sqlite3.Connection) -> int:
    """Subscribe this machine's live sessions to the chatroom, so sessions that were
    open before an upgrade are in it. Returns how many sessions were subscribed.
    """
    rows = conn.execute(
        "SELECT session_id FROM sessions WHERE status='active' AND owner_pid IS NOT NULL "
        "AND client NOT LIKE ? AND last_seen_at>=?",
        (REMOTE_PREFIX + "%", _now() - ACTIVE_WINDOW),
    ).fetchall()
    for r in rows:
        _join_room(conn, r["session_id"])
    conn.commit()
    return len(rows)


def touch(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET last_seen_at=?, status='active' WHERE session_id=?",
        (_now(), session_id),
    )
    conn.commit()


def _throttle_due(conn: sqlite3.Connection, session_id: str, column: str, interval: float) -> bool:
    """Shared check-and-set behind the per-session time throttles. True if at least
    ``interval`` seconds have passed since ``column`` was last stamped for this
    session, writing ``now`` into it when so. The caller acts iff this returns True,
    so the stamp and the action can't drift apart.

    An unknown session, or ``interval`` <= 0, returns False without stamping — the
    first is nothing to key on, the second is the feature's "off" setting. ``column``
    is a fixed internal identifier, never caller input, so it is safe to interpolate.
    """
    if interval <= 0:
        return False
    row = conn.execute(
        f"SELECT {column} FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        return False
    now = _now()
    last = row[column]
    if last is not None and now - last < interval:
        return False
    conn.execute(f"UPDATE sessions SET {column}=? WHERE session_id=?", (now, session_id))
    conn.commit()
    return True


def timeline_due(conn: sqlite3.Connection, session_id: str, interval: float) -> bool:
    """True (once per ``interval``) when the mid-turn wall-clock timeline line is
    due for this session; see notify.timeline_text. Stamps ``time_injected_at``.
    """
    return _throttle_due(conn, session_id, "time_injected_at", interval)


def heartbeat_due(conn: sqlite3.Connection, session_id: str, interval: float) -> bool:
    """True (once per ``interval``) when the at-idle Stop-hook heartbeat nudge is
    due for this session; see notify.heartbeat_reason. Stamps ``heartbeat_at``.
    """
    return _throttle_due(conn, session_id, "heartbeat_at", interval)


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


def set_role(
    conn: sqlite3.Connection,
    session_id: str,
    role: str | None,
    responsibilities: str | None = None,
) -> None:
    """Set (or clear) this session's role and its responsibilities. Passing
    role=None clears both. Callers resolve the roster default for
    `responsibilities` before calling; this writes exactly what it is given.
    """
    conn.execute(
        "UPDATE sessions SET role=?, responsibilities=?, last_seen_at=? WHERE session_id=?",
        (role, responsibilities if role else None, _now(), session_id),
    )
    conn.commit()


def agents_with_role(
    conn: sqlite3.Connection, role: str, project: str | None = None, exclude: str | None = None
) -> list[sqlite3.Row]:
    """Active sessions whose role matches `role` (case-insensitive), most
    recently seen first. The routing primitive: 'who on the bus owns this?'.
    """
    return [
        r
        for r in active_sessions(conn, project=project, exclude=exclude)
        if (r["role"] or "").strip().lower() == role.strip().lower()
    ]


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


# --------------------------------------------------------------------------- #
# Remote sessions: agents on a paired machine (see lan.Service). Stored as
# sessions rows with client='lan:<node>' and no pid, so the reaper, ``_me`` and
# the guard skip them, while list_agents and send resolve them unchanged.
# --------------------------------------------------------------------------- #
REMOTE_PREFIX = "lan:"
_REMOTE_COLS = ("session_id", "handle", "project_name", "model", "role", "last_seen_at")


def export_sessions(conn: sqlite3.Connection, exclude: set[str]) -> list[dict[str, Any]]:
    """This machine's own live agents, for a paired peer. Never re-exports remote
    rows, and leaves out projects in ``exclude``.
    """
    cutoff = _now() - ACTIVE_WINDOW
    rows = conn.execute(
        f"SELECT {', '.join(_REMOTE_COLS)} FROM sessions WHERE status='active' "
        "AND last_seen_at>=? AND client NOT LIKE ? AND owner_pid IS NOT NULL",
        (cutoff, REMOTE_PREFIX + "%"),
    ).fetchall()
    return [dict(r) for r in rows if (r["project_name"] or "").lower() not in exclude]


def replace_remote_sessions(conn: sqlite3.Connection, node: str, rows: list[dict[str, Any]]) -> int:
    """Make ``node``'s remote rows exactly ``rows``. A local row with the same id is
    never touched. Returns how many rows were stored.
    """
    client = REMOTE_PREFIX + node
    conn.execute("DELETE FROM sessions WHERE client=?", (client,))
    n = 0
    for r in rows:
        sid, name = str(r.get("session_id") or ""), r.get("project_name")
        if not sid:
            continue
        # list_agents filters on the local project path: borrow it from a local
        # session of the same project, so remote agents show up by default.
        local = conn.execute(
            "SELECT project FROM sessions WHERE project_name=? AND client NOT LIKE ? "
            "AND project IS NOT NULL ORDER BY last_seen_at DESC LIMIT 1",
            (name, REMOTE_PREFIX + "%"),
        ).fetchone()
        seen = float(r.get("last_seen_at") or _now())
        cur = conn.execute(
            "INSERT OR IGNORE INTO sessions(session_id, handle, project, project_name, "
            "client, model, role, status, created_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,'active',?,?)",
            (
                sid,
                r.get("handle"),
                local["project"] if local else None,
                name,
                client,
                r.get("model"),
                r.get("role"),
                seen,
                seen,
            ),
        )
        n += cur.rowcount
    conn.commit()
    return n


def prune_remote_sessions(conn: sqlite3.Connection) -> int:
    """Delete remote rows not seen for ``ACTIVE_WINDOW`` (the peer stopped answering)."""
    cur = conn.execute(
        "DELETE FROM sessions WHERE client LIKE ? AND last_seen_at<?",
        (REMOTE_PREFIX + "%", _now() - ACTIVE_WINDOW),
    )
    conn.commit()
    return cur.rowcount


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


def presence_state(row: sqlite3.Row | None, now: float | None = None) -> str:
    """Derived presence for a session from status + last_seen_at: working (a tool
    call pushed very recently), idle (active but quiet), stale (past the active
    window, a reaper will retire it), or ended. Richer than online/offline, and
    free: last_seen_at is refreshed by every hook, so no new state machine.
    """
    if row is None:
        return "unknown"
    if row["status"] == "ended":
        return "ended"
    now = _now() if now is None else now
    age = now - (row["last_seen_at"] or 0)
    if age <= WORKING_WINDOW:
        return "working"
    if age <= ACTIVE_WINDOW:
        return "idle"
    return "stale"


def sessions_since(conn: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    """Every session seen at or after `since`, active or ended. The transcript
    ingest walks these: an ended session still has a tail to capture (its last
    turns push after Stop), so this is not limited to active ones.
    """
    return conn.execute(
        "SELECT * FROM sessions WHERE last_seen_at>=? ORDER BY last_seen_at DESC",
        [since],
    ).fetchall()


def set_session_model(conn: sqlite3.Connection, session_id: str, model: str) -> None:
    """Record the model a session is running (an agent-card field). No-op when
    unchanged, so the common ingest path does not churn the row.
    """
    conn.execute(
        "UPDATE sessions SET model=? WHERE session_id=? AND (model IS NULL OR model!=?)",
        [model, session_id, model],
    )
    conn.commit()


def add_decision(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str | None = None,
    kind: str = "decision",
    refs: str | None = None,
    tags: str | None = None,
    session_id: str | None = None,
    handle: str | None = None,
    project: str | None = None,
    project_name: str | None = None,
) -> int:
    """Record a durable decision and return its id."""
    cur = conn.execute(
        "INSERT INTO decisions (created_at, session_id, handle, project, project_name, "
        "kind, title, body, refs, tags) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [_now(), session_id, handle, project, project_name, kind, title, body, refs, tags],
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_decisions(
    conn: sqlite3.Connection,
    *,
    project_name: str | None = None,
    kind: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Durable decisions, newest first. `query` matches a substring in the title,
    body, refs or tags, so `read_decisions("auth")` returns what was decided about auth.
    """
    q = "SELECT * FROM decisions WHERE 1=1"
    args: list[Any] = []
    if project_name:
        q += " AND project_name=?"
        args.append(project_name)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if query:
        q += " AND (title LIKE ? OR body LIKE ? OR refs LIKE ? OR tags LIKE ?)"
        args += [f"%{query}%"] * 4
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args)]


# Session notes (issue #34). The vocabularies live here so the store, the MCP
# tools and the CLI all validate against one source.
NOTE_TYPES = ("topic", "need", "bug", "idea", "unfinished")
NOTE_STATES = ("open", "done", "parked")


def add_note(
    conn: sqlite3.Connection,
    *,
    text: str,
    type: str = "topic",
    session_id: str | None = None,
    handle: str | None = None,
    project: str | None = None,
    project_name: str | None = None,
) -> int:
    """Record a session note (a loose end) and return its id. An unknown `type`
    falls back to 'topic'; a new note is always 'open'.
    """
    note_type = type if type in NOTE_TYPES else "topic"
    now = _now()
    cur = conn.execute(
        "INSERT INTO session_notes (created_at, updated_at, session_id, handle, project, "
        "project_name, type, text, state, parked_ref) VALUES (?,?,?,?,?,?,?,?,'open',NULL)",
        [now, now, session_id, handle, project, project_name, note_type, text],
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_notes(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
    state: str | None = None,
    type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Session notes, OLDEST first — the order the loose ends arose, which is how
    `/wrap-up` wants to walk them. Filter by `session_id` (one session's notes),
    `project_name`, `state` (open/done/parked) and `type`.
    """
    q = "SELECT * FROM session_notes WHERE 1=1"
    args: list[Any] = []
    if session_id:
        q += " AND session_id=?"
        args.append(session_id)
    if project_name:
        q += " AND project_name=?"
        args.append(project_name)
    if state:
        q += " AND state=?"
        args.append(state)
    if type:
        q += " AND type=?"
        args.append(type)
    q += " ORDER BY id ASC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args)]


def set_note_state(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    state: str,
    parked_ref: str | None = None,
) -> dict[str, Any] | None:
    """Move a note to `state` (open/done/parked), stamping `updated_at` and, when
    parking, `parked_ref` (where it went). Returns the updated row, or None if no
    note has that id or `state` is not a known state.
    """
    if state not in NOTE_STATES:
        return None
    cur = conn.execute(
        "UPDATE session_notes SET state=?, parked_ref=?, updated_at=? WHERE id=?",
        [state, parked_ref, _now(), note_id],
    )
    conn.commit()
    if not cur.rowcount:
        return None
    row = conn.execute("SELECT * FROM session_notes WHERE id=?", [note_id]).fetchone()
    return dict(row) if row else None


def get_ingest_cursor(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT byte_offset FROM ingest_cursors WHERE session_id=?", [session_id]
    ).fetchone()
    return int(row["byte_offset"]) if row else 0


def set_ingest_cursor(conn: sqlite3.Connection, session_id: str, byte_offset: int) -> None:
    conn.execute(
        "INSERT INTO ingest_cursors (session_id, byte_offset, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET byte_offset=excluded.byte_offset, "
        "updated_at=excluded.updated_at",
        [session_id, byte_offset, _now()],
    )
    conn.commit()


def record_transcript_messages(
    conn: sqlite3.Connection,
    session_id: str,
    rows: list[dict[str, Any]],
    *,
    client: str = "claude",
    project: str | None = None,
    project_name: str | None = None,
) -> int:
    """Persist conversation turns. Each row is {uuid, role, text, ts, model}.
    INSERT OR IGNORE keyed on (session_id, uuid), so re-ingesting a transcript
    never double-writes. Returns how many new rows pushed.
    """
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO transcript_messages "
        "(session_id, uuid, ts, role, model, client, project, project_name, body) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                session_id,
                r["uuid"],
                r.get("ts"),
                r["role"],
                r.get("model"),
                client,
                project,
                project_name,
                r["text"],
            )
            for r in rows
            if r.get("uuid") and r.get("role") and r.get("text")
        ],
    )
    conn.commit()
    return conn.total_changes - before


def query_transcript_messages(
    conn: sqlite3.Connection,
    *,
    project_name: str | None = None,
    session_id: str | None = None,
    role: str | None = None,
    grep: str | None = None,
    since: float | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Recorded turns, newest first, filtered by any combination of project,
    session, role, a substring in the body, and a time floor. `limit` caps it.
    """
    q = "SELECT session_id, ts, role, model, client, project_name, body FROM transcript_messages WHERE 1=1"
    args: list[Any] = []
    if project_name:
        q += " AND project_name=?"
        args.append(project_name)
    if session_id:
        q += " AND session_id=?"
        args.append(session_id)
    if role:
        q += " AND role=?"
        args.append(role)
    if grep:
        q += " AND body LIKE ?"
        args.append(f"%{grep}%")
    if since is not None:
        q += " AND ts>=?"
        args.append(since)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    return conn.execute(q, args).fetchall()


def transcript_messages_between(
    conn: sqlite3.Connection, project_name: str | None, start: float, end: float
) -> list[sqlite3.Row]:
    """Every recorded turn for a project whose ts is in [start, end), grouped by
    session and then in time order — the shape a day's minutes render from.
    """
    return conn.execute(
        "SELECT session_id, ts, role, model, body FROM transcript_messages "
        "WHERE project_name=? AND ts>=? AND ts<? ORDER BY session_id, ts",
        [project_name, start, end],
    ).fetchall()


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
    provenance: str = "local",
    guid: str | None = None,
    created_at: float | None = None,
) -> int:
    """Insert a message and return its local id.

    `guid` is the global identity; a fresh one is minted for a locally-born
    message, but the LAN and WAN transports pass the original so the same message
    is not re-inserted on another machine. Insert is idempotent
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
        "project, body, urgent, provenance, guid, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
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
            provenance,
            guid,
            _now(),
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
        "WHERE s.session_id=? AND s.topic=m.topic AND COALESCE(m.received_at, m.created_at) >= s.joined_at)) "
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


def inbox_after(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_id: int = 0,
    urgent_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Inbox messages with ``id > after_id``, oldest first — the cursor feed a
    push-delivery poller drains.

    Same addressed-to-me predicate as `inbox`, but keyed on the monotonic
    ``id`` instead of read-state or a time window, so a poller never double-
    sees a row and never misses one (gap-free by construction). Read-state
    rides on each row (``read``) for the caller to gate on, but does not
    filter here: the cursor is the poller's own "have I noticed this row yet"
    pointer, independent of the `reads` table.
    """
    where, args = _inbox_where(session_id)
    q = (
        "SELECT m.*, r.read_at AS read_at FROM messages m "
        "LEFT JOIN reads r ON r.message_id=m.id AND r.session_id=? "
        f"WHERE {where} AND m.id > ?"
    )
    args = [session_id, *args, after_id]
    if urgent_only:
        q += " AND m.urgent=1"
    q += " ORDER BY m.id ASC LIMIT ?"
    args.append(limit)
    return [_msg_dict(r) for r in conn.execute(q, args)]


def max_message_id(conn: sqlite3.Connection) -> int:
    """The newest message id on the bus, or 0 when there are none. A poller
    seeds its cursor here so it wakes on what arrives *after* it starts, not on
    the whole backlog.
    """
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM messages").fetchone()["m"])


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
        "SELECT t.name, t.project, t.gated, "
        "(SELECT COUNT(*) FROM subscriptions s WHERE s.topic=t.name) AS subscribers, "
        "(SELECT COUNT(*) FROM messages m WHERE m.topic=t.name) AS messages "
        "FROM topics t ORDER BY t.name"
    ).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["gated"] = bool(r["gated"])
    return out


def set_topic_gated(conn: sqlite3.Connection, topic: str, gated: bool = True) -> None:
    """Mark a topic private (gated=1, a listen/send grant required to join/post)
    or open (gated=0). Ensures the topic exists first.
    """
    ensure_topic(conn, topic)
    conn.execute("UPDATE topics SET gated=? WHERE name=?", (1 if gated else 0, topic))
    conn.commit()


def topic_is_gated(conn: sqlite3.Connection, topic: str) -> bool:
    row = conn.execute("SELECT gated FROM topics WHERE name=?", (topic,)).fetchone()
    return bool(row["gated"]) if row else False


# --------------------------------------------------------------------------- #
# Grants (cross-project consent) + directory (discovery) — issue #26
# --------------------------------------------------------------------------- #
GRANT_CAPS = ("listen", "send")


def _norm_grant_resource(kind: str, resource: str) -> str:
    """Project and topic names are case-insensitive; a session id (agent) is
    opaque and kept verbatim.
    """
    r = (resource or "").strip()
    return r.lower() if kind in ("project", "topic") else r


def upsert_grant(
    conn: sqlite3.Connection,
    *,
    grantee: str,
    resource_kind: str,
    resource: str,
    capability: str,
    status: str,
    by: str | None = None,
    requested_by: str | None = None,
    note: str | None = None,
) -> int:
    """Insert or update the single grant row for (grantee, kind, resource,
    capability) and return its id. `status='requested'` stamps `requested_by`;
    any decided status stamps `decided_by` (from `by`) and `decided_at`.
    """
    grantee = (grantee or "").strip().lower()
    resource = _norm_grant_resource(resource_kind, resource)
    now = _now()
    decided_at = None if status == "requested" else now
    conn.execute(
        "INSERT INTO grants(grantee,resource_kind,resource,capability,status,"
        "requested_by,decided_by,note,created_at,decided_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(grantee,resource_kind,resource,capability) DO UPDATE SET "
        "status=excluded.status, "
        "requested_by=COALESCE(excluded.requested_by, grants.requested_by), "
        "decided_by=COALESCE(excluded.decided_by, grants.decided_by), "
        "note=COALESCE(excluded.note, grants.note), "
        "decided_at=excluded.decided_at",
        (
            grantee,
            resource_kind,
            resource,
            capability,
            status,
            requested_by,
            by,
            note,
            now,
            decided_at,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM grants WHERE grantee=? AND resource_kind=? AND resource=? AND capability=?",
        (grantee, resource_kind, resource, capability),
    ).fetchone()
    return int(row["id"])


def get_grant(conn: sqlite3.Connection, grant_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
    return dict(row) if row else None


def decide_grant(
    conn: sqlite3.Connection, grant_id: int, *, status: str, by: str | None
) -> dict[str, Any] | None:
    """Move a grant to a decided status (granted|denied|revoked). Returns the
    updated row, or None if no such grant.
    """
    if conn.execute("SELECT 1 FROM grants WHERE id=?", (grant_id,)).fetchone() is None:
        return None
    conn.execute(
        "UPDATE grants SET status=?, decided_by=?, decided_at=? WHERE id=?",
        (status, by, _now(), grant_id),
    )
    conn.commit()
    return get_grant(conn, grant_id)


def has_grant(
    conn: sqlite3.Connection, grantee: str, resource_kind: str, resource: str, capability: str
) -> bool:
    """True iff a GRANTED row lets `grantee` reach `resource` with `capability`."""
    grantee = (grantee or "").strip().lower()
    resource = _norm_grant_resource(resource_kind, resource)
    row = conn.execute(
        "SELECT 1 FROM grants WHERE grantee=? AND resource_kind=? AND resource=? "
        "AND capability=? AND status='granted'",
        (grantee, resource_kind, resource, capability),
    ).fetchone()
    return row is not None


def _reach_ok(
    conn: sqlite3.Connection, mine: str | None, target: str | None, capability: str
) -> bool:
    """Whether a session in project `mine` may reach project `target` with
    `capability` ('listen'|'send'): your own project, a linked peer, or a
    granted directional row. A session with no project identity is unrestricted.
    """
    m = (mine or "").strip().lower()
    t = (target or "").strip().lower()
    if not m or not t or m == t:
        return True
    try:
        from . import cloud

        if t in cloud.peers_visible(m):
            return True
    except Exception:
        pass
    return has_grant(conn, m, "project", t, capability)


def _proj_topic(topic: str | None) -> str | None:
    t = (topic or "").lstrip("#").strip()
    return t[len("proj:") :].strip().lower() if t.startswith("proj:") else None


def share_a_room(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """Whether two sessions are both in the same chatroom (any topic that is not a
    ``proj:`` channel). Joining a room is opting in to talk, so a DM between two
    members needs no project link or grant.
    """
    row = conn.execute(
        "SELECT 1 FROM subscriptions x JOIN subscriptions y ON x.topic=y.topic "
        "WHERE x.session_id=? AND y.session_id=? AND x.topic NOT LIKE 'proj:%' LIMIT 1",
        (a, b),
    ).fetchone()
    return row is not None


def consent_error(
    conn: sqlite3.Connection,
    sid: str,
    *,
    to_session: str | None = None,
    topic: str | None = None,
) -> str | None:
    """The cross-project consent + gated-topic check (issue #26), shared by the
    MCP `send` tool and the `cabildo send` CLI so the CLI cannot bypass it.
    Returns a refusal message, or None when the send is allowed.
    """
    row = get_session(conn, sid)
    mine = row["project_name"] if row else None
    if to_session is not None:
        trow = get_session(conn, to_session)
        tname = (trow["project_name"] if trow else None) or ""
        # A paired machine's agent is reachable from anywhere: the pairing is the
        # consent, and its project name means nothing on this machine.
        if trow is not None and str(trow["client"] or "").startswith(REMOTE_PREFIX):
            return None
        if share_a_room(conn, sid, to_session):
            return None
        if not _reach_ok(conn, mine, tname, "send"):
            return (
                f"'{to_session}' is on project '{tname.strip().lower()}', not reachable "
                f"from yours — request_access for a grant, or link the projects"
            )
        return None
    pj = _proj_topic(topic)
    if pj is not None:
        if not _reach_ok(conn, mine, pj, "send"):
            return (
                f"#{topic} is project '{pj}'s channel, not reachable from yours — "
                f"request_access for a grant, or link the projects"
            )
        return None
    if topic and topic_is_gated(conn, topic) and (mine or "").strip():
        subscribed = topic in subscriptions_of(conn, sid)
        if not subscribed and not has_grant(
            conn, (mine or "").strip().lower(), "topic", topic, "send"
        ):
            return f"#{topic} is a gated topic — request_access for a 'send' grant"
    return None


def list_grants(
    conn: sqlite3.Connection,
    *,
    grantee: str | None = None,
    resource_kind: str | None = None,
    resource: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Grant rows, newest first, narrowed by any of grantee/kind/resource/status."""
    where: list[str] = []
    args: list[Any] = []
    if grantee is not None:
        where.append("grantee=?")
        args.append(grantee.strip().lower())
    if resource_kind is not None:
        where.append("resource_kind=?")
        args.append(resource_kind)
    if resource is not None and resource_kind is not None:
        where.append("resource=?")
        args.append(_norm_grant_resource(resource_kind, resource))
    if status is not None:
        where.append("status=?")
        args.append(status)
    sql = "SELECT * FROM grants"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def directory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read-only roster of the projects active on the bus: each project's name,
    its active-agent count, and the roles present. No message content and no
    channel contents — this is discovery, not delivery, so any agent may read it
    without a grant.
    """
    rows = conn.execute(
        "SELECT project_name AS project, COUNT(*) AS agents, "
        "GROUP_CONCAT(DISTINCT role) AS roles "
        "FROM sessions WHERE status='active' AND COALESCE(project_name,'')<>'' "
        "GROUP BY lower(project_name) ORDER BY agents DESC, project_name"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        roles = sorted({x for x in (r["roles"] or "").split(",") if x})
        out.append({"project": r["project"], "agents": r["agents"], "roles": roles})
    return out


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


def spawn_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``session_id -> its spawn record`` (role, parent_session, depth, status), so
    a caller can mark which live sessions are spawned sub-agents and how deep. Only
    rows whose session has registered (session_id backfilled); latest row wins when
    a session_id recurs (a reused worktree).
    """
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT session_id, parent_session, role, depth, status FROM spawns "
        "WHERE session_id IS NOT NULL ORDER BY created_at"
    ):
        out[r["session_id"]] = dict(r)
    return out


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
def _git_common_dir(cwd: str) -> str | None:
    """Return the absolute git common dir of ``cwd``, shared by all its worktrees."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def intent_lane(action: str, project: str | None, params: dict[str, Any]) -> str:
    """Return the lane an intent runs in; one lane runs one intent at a time.

    A push's lane is the git repo it pushes from, not the project name: two
    pinned packages of one monorepo, and agent worktrees of one repo, share a
    working tree or a remote branch, so their pushes must not run together.
    Every other action shares one lane: spawn checks a single ``max_agents`` cap.
    """
    if action == "pair":
        return "lan"  # waits up to 20 s for a beacon; must not hold up spawn
    if action != "push":
        return "*"
    cwd = params.get("cwd")
    repo = _git_common_dir(str(cwd)) if cwd else None
    return f"push:{repo or project or ''}"


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
        "params, lane) VALUES (?,?,?,?,?,?,?)",
        (
            _now(),
            from_session,
            from_handle,
            project,
            action,
            json.dumps(params),
            intent_lane(action, project, params),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


# Rows queued before the lane column existed fall back to a lane per project.
_INTENT_LANE = (
    "COALESCE({t}.lane, CASE WHEN {t}.action='push' "
    "THEN 'push:' || COALESCE({t}.project, '') ELSE '*' END)"
)


def take_next_intent(conn: sqlite3.Connection, worker: str) -> dict[str, Any] | None:
    """Atomically claim the oldest pending intent whose lane has nothing running.

    One UPDATE..RETURNING rather than SELECT-then-UPDATE: the read and the write
    are a single statement, so two workers can never take the same row, nor two
    rows of the same lane. Intents in different lanes may run at the same time.
    """
    row = conn.execute(
        "UPDATE intents SET status='running', worker=?, started_at=?, attempts=attempts+1 "
        "WHERE id = (SELECT p.id FROM intents p WHERE p.status='pending' AND NOT EXISTS ("
        "SELECT 1 FROM intents r WHERE r.status='running' "
        f"AND {_INTENT_LANE.format(t='r')} = {_INTENT_LANE.format(t='p')}) "
        "ORDER BY p.id LIMIT 1) "
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
    # tell those apart. 'dry_run' (the daemon isn't pushing) and 'unverified' (the
    # remote couldn't be checked) are likewise distinct from a real 'done' so
    # `done` always means "pushed on the remote" (issue #15).
    if status not in ("done", "failed", "rejected", "expired", "dry_run", "unverified"):
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


def reap_stale_intents(
    conn: sqlite3.Connection, older_than: float, *, worker: str | None = None
) -> int:
    """A daemon killed mid-action leaves rows stuck in 'running' forever. Fail
    them loudly rather than leaving a queue that silently never drains: a stuck
    row blocks every later intent of its lane.

    With ``worker``, fail that worker's running rows whatever their age (a
    daemon exiting with actions it will never finish).
    """
    if worker is not None:
        where, args = "status='running' AND worker=?", (worker,)
    else:
        where, args = "status='running' AND started_at < ?", (_now() - older_than,)
    cur = conn.execute(
        "UPDATE intents SET status='failed', finished_at=?, "
        "result=json_object('error', 'daemon died while running this intent') "
        f"WHERE {where}",
        (_now(), *args),
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


def file_pattern_matches(pattern: str, candidates: list[str]) -> bool:
    """Does a file:<pattern> claim cover any of these paths? A pattern ending in
    "/" is a directory-prefix claim covering the whole subtree; anything else is
    an fnmatch glob or an exact path. Shared by the edit guard and the pusher so
    both agree on what a claim locks.
    """
    from fnmatch import fnmatch

    if pattern.endswith("/"):
        base = pattern.rstrip("/")
        return any(c == base or c.startswith(pattern) for c in candidates)
    return any(fnmatch(c, pattern) for c in candidates)


def file_claim_holders(
    conn: sqlite3.Connection,
    project: str | None,
    files: list[str],
    *,
    exclude_session: str | None = None,
) -> list[dict[str, Any]]:
    """Active file claims (held by anyone but exclude_session) that overlap any of
    `files` (repo-relative paths). Used to tell a pusher whose in-flight work its
    rebase conflict collides with, so it knows whom to coordinate with.
    """
    now = _now()
    hits: list[dict[str, Any]] = []
    q = "SELECT * FROM claims WHERE resource LIKE 'file:%' AND expires_at>?"
    args: list[Any] = [now]
    if project:
        q += " AND project=?"
        args.append(project)
    for r in conn.execute(q, args):
        if exclude_session and r["holder_session"] == exclude_session:
            continue
        pattern = r["resource"][len("file:") :]
        hit = next((f for f in files if file_pattern_matches(pattern, [f])), None)
        if hit:
            hits.append(
                {
                    "resource": r["resource"],
                    "file": hit,
                    "holder": display_name(
                        get_session(conn, r["holder_session"]), r["holder_session"]
                    ),
                }
            )
    return hits


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


# --------------------------------------------------------------------------- #
# Schedule / wind-down (the attention engine's stop lines)
# --------------------------------------------------------------------------- #
# How long after its line a stop stays in force. A stop at 23:00 keeps nudging
# "you're past your stop" into the small hours if the human works on; the next
# day's stop supersedes it, but absent that we stop honouring a line this long
# past it so a forgotten stop doesn't haunt tomorrow morning.
SCHEDULE_STALE_AFTER = 8 * 3600.0


def set_stop(
    conn: sqlite3.Connection,
    *,
    line_at: float,
    scope: str = "global",
    created_by: str | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Set the stop line for ``scope`` on this host, retiring any prior active
    stop for the same scope (one line per scope — a new one replaces it rather
    than stacking). Returns the new row.
    """
    host = host or hostname()
    now = _now()
    conn.execute(
        "UPDATE schedule SET active=0 WHERE host=? AND scope=? AND kind='stop' AND active=1",
        (host, scope),
    )
    cur = conn.execute(
        "INSERT INTO schedule(host, scope, kind, label, line_at, ends_at, created_by, "
        "created_at, active) VALUES (?,?,'stop',NULL,?,NULL,?,?,1)",
        (host, scope, line_at, created_by, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM schedule WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def clear_stops(
    conn: sqlite3.Connection, *, scope: str | None = None, host: str | None = None
) -> int:
    """Retire active stop lines on this host, optionally only for one ``scope``.
    Returns how many were retired.
    """
    host = host or hostname()
    q = "UPDATE schedule SET active=0 WHERE host=? AND kind='stop' AND active=1"
    params: list[Any] = [host]
    if scope is not None:
        q += " AND scope=?"
        params.append(scope)
    cur = conn.execute(q, params)
    conn.commit()
    return cur.rowcount


def active_stops(conn: sqlite3.Connection, *, host: str | None = None) -> list[dict[str, Any]]:
    """Active, non-stale stop lines on this host, soonest line first."""
    host = host or hostname()
    now = _now()
    rows = conn.execute(
        "SELECT * FROM schedule WHERE host=? AND kind='stop' AND active=1 "
        "AND line_at > ? ORDER BY line_at ASC",
        (host, now - SCHEDULE_STALE_AFTER),
    ).fetchall()
    return [dict(r) for r in rows]


def governing_stop(
    conn: sqlite3.Connection, session_id: str, *, host: str | None = None
) -> dict[str, Any] | None:
    """The stop line a given session is steered by: the soonest active line
    whose scope is ``global`` or matches the session's own project. None when no
    line governs it — which is most of the time, so the nudge stays silent.
    """
    me = get_session(conn, session_id)
    project = me["project"] if me else None
    for row in active_stops(conn, host=host):
        if row["scope"] == "global" or (project and row["scope"] == project):
            return row
    return None


def winddown_due(conn: sqlite3.Connection, schedule_id: int, session_id: str, step: int) -> bool:
    """True if ``step`` is a NEW escalation step for this session on this line
    (strictly past the highest it has been shown), recording it when so. Step 0
    (outside the window) is never due. The record-on-true keeps the stamp and the
    nudge from drifting apart, exactly like ``_throttle_due``.
    """
    if step <= 0:
        return False
    row = conn.execute(
        "SELECT step FROM schedule_deliveries WHERE schedule_id=? AND session_id=?",
        (schedule_id, session_id),
    ).fetchone()
    if row is not None and row["step"] >= step:
        return False
    conn.execute(
        "INSERT INTO schedule_deliveries(schedule_id, session_id, step, delivered_at) "
        "VALUES (?,?,?,?) ON CONFLICT(schedule_id, session_id) DO UPDATE SET "
        "step=excluded.step, delivered_at=excluded.delivered_at",
        (schedule_id, session_id, step, _now()),
    )
    conn.commit()
    return True
