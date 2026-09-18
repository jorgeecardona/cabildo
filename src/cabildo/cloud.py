"""Push the local SQLite bus up to the hosted App Engine mirror.

The hosted dashboard only knows about agents/messages/locks that were reported to
it; your local Claude sessions are invisible until something mirrors them. The
`webapp/runner.py` daemon does this as part of its machine-registration loop;
this module is the same one-way (local -> cloud) mirror exposed as `cabildo sync`
so a human can push from any terminal without the full runner.

Config comes from, in order: explicit args, environment (CABILDO_TOKEN /
CABILDO_BASE), then ~/.cabildo/cloud.json ({"token": ..., "base": ...}) written
once by `cabildo sync --set-token`. Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import store

DEFAULT_BASE = "https://cabildo-503120.ew.r.appspot.com"


# --------------------------------------------------------------------------- #
# Config — a tiny 600-mode json file so the token never rides on argv/history.
# --------------------------------------------------------------------------- #
def config_path() -> Path:
    return Path(os.path.expanduser("~/.cabildo/cloud.json"))


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except Exception:
        return {}


def save_token(token: str, base: str | None = None) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg["token"] = token
    cfg["base"] = base or cfg.get("base") or DEFAULT_BASE
    p.write_text(json.dumps(cfg, indent=2))
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 600 — it holds a secret
    return p


def resolve(token: str | None = None, base: str | None = None) -> tuple[str, str]:
    """(base, token) from args > env > cloud.json; raises if no token anywhere."""
    cfg = load_config()
    token = token or os.environ.get("CABILDO_TOKEN") or cfg.get("token")
    base = base or os.environ.get("CABILDO_BASE") or cfg.get("base") or DEFAULT_BASE
    if not token:
        raise RuntimeError(
            "no cloud token — run `cabildo sync --set-token <token>` once, or set CABILDO_TOKEN"
        )
    return base.rstrip("/"), token


# --------------------------------------------------------------------------- #
# Project registry — a first-class list of projects (each a folder on this
# machine), each with two dials the owner controls:
#
#   uplink : how much of the project reaches the hosted cloud. A ladder, so a
#            higher rung implies every lower one:
#              none      — the project never appears in the cloud at all
#              presence  — its agents + locks show up; no message bodies
#              messages  — its inter-agent bus messages too
#              content   — its full conversation (your prompts + Claude's
#                          replies) — the private half of a session
#   peers  : which OTHER projects this one may see/talk to on the local bus.
#            Symmetric: adding B to A's peers adds A to B's. Default: none, so
#            projects are siloed unless you explicitly link them.
#
# Isolation is the default: a project not listed here gets `default_uplink`
# (presence — the dashboard sees it's alive but no words leave) and no peers.
# Conversation content NEVER leaves the machine unless a project is explicitly
# raised to `content`, which is how the personal "second-brain" sessions stay
# put. Names are stored lowercased so matching a session's project_name is
# case-insensitive. The registry lives in cloud.json beside the token (600).
# --------------------------------------------------------------------------- #
UPLINK_LEVELS = ("none", "presence", "messages", "content")
_UPLINK_RANK = {lvl: i for i, lvl in enumerate(UPLINK_LEVELS)}
DEFAULT_UPLINK = "presence"


def _norm(name: Any) -> str:
    return str(name or "").strip().lower()


def _norm_root(path: Any) -> str:
    """A folder as the registry stores it: absolute, ~-expanded, symlinks
    resolved, no trailing slash. NOT lowercased — paths are case-sensitive here,
    and a registry that quietly renamed your directories would be a trap.
    """
    p = str(path or "").strip()
    if not p:
        return ""
    return os.path.realpath(os.path.expanduser(p))


def default_uplink() -> str:
    """The uplink applied to any project not explicitly in the registry."""
    lvl = _norm(load_config().get("default_uplink"))
    return lvl if lvl in _UPLINK_RANK else DEFAULT_UPLINK


def _registry(cfg: dict | None = None) -> dict[str, dict]:
    """The normalized project map, folding any legacy `share_projects` list in as
    content-level entries so old configs keep working.
    """
    cfg = cfg if cfg is not None else load_config()
    raw = cfg.get("projects")
    raw = dict(raw) if isinstance(raw, dict) else {}
    for name in cfg.get("share_projects") or []:  # migrate the old boolean list
        n = _norm(name)
        if n and n not in raw:
            raw[n] = {"uplink": "content"}
    out: dict[str, dict] = {}
    for name, e in raw.items():
        n = _norm(name)
        if not n:
            continue
        e = e if isinstance(e, dict) else {}
        lvl = _norm(e.get("uplink"))
        peers = e.get("peers")
        roots = e.get("roots")
        out[n] = {
            "uplink": lvl if lvl in _UPLINK_RANK else "",  # "" ⇒ fall back to default
            "peers": sorted({_norm(p) for p in peers if _norm(p)})
            if isinstance(peers, list)
            else [],
            "roots": sorted({_norm_root(r) for r in roots if _norm_root(r)})
            if isinstance(roots, list)
            else [],
        }
    return out


def project_registry() -> dict[str, dict]:
    """Public view of the registry: {name: {"uplink", "peers", "roots"}}."""
    return _registry()


def _save_registry(reg: dict[str, dict]) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg["projects"] = {
        n: {
            "uplink": e.get("uplink") or default_uplink(),
            "peers": sorted(set(e.get("peers") or [])),
            "roots": sorted(set(e.get("roots") or [])),
        }
        for n, e in reg.items()
    }
    cfg.pop("share_projects", None)  # fully migrated into projects
    p.write_text(json.dumps(cfg, indent=2))
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 600 — sits beside the token
    return p


# Rows on the bus identify their project two different ways: sessions carry a
# `project_name` ("paas"), while messages and claims carry `project`, an absolute
# path. The dials are keyed by name, so every path-keyed lookup silently missed
# the registry and fell through to the default — the dial existed and did
# nothing. It failed closed for messages (nothing left when it should have) and
# open for claims (a `none` project's locks would still be pushed). One
# normalizer, applied inside the lookup, so no caller has to remember which
# shape it is holding.
_KEY_TTL = 10.0
_key_cache: dict[str, tuple[float, str]] = {}


def project_key(value: Any) -> str:
    """The registry key for a project named either way: a name stays a name, an
    absolute path resolves through the same logic a session uses (pins, linked
    worktrees, git root) so `/…/paas/.claude/worktrees/x` is still `paas`.
    """
    v = str(value or "").strip()
    if not v.startswith("/"):
        return _norm(v)
    now = time.time()
    hit = _key_cache.get(v)
    if hit and now - hit[0] < _KEY_TTL:
        return hit[1]
    try:
        from . import context

        name = _norm(context.git_project(v)[1])
    except Exception:
        name = _norm(os.path.basename(v.rstrip("/")))
    _key_cache[v] = (now, name)
    return name


def uplink_of(name: Any) -> str:
    """The effective uplink level for a project (its own, or the default).
    Accepts a project name or a project path.
    """
    e = _registry().get(project_key(name))
    lvl = _norm(e.get("uplink")) if e else ""
    return lvl if lvl in _UPLINK_RANK else default_uplink()


def uplink_allows(name: Any, need: str) -> bool:
    """True if `name`'s uplink is at least `need` on the ladder."""
    return _UPLINK_RANK.get(uplink_of(name), 0) >= _UPLINK_RANK.get(need, 99)


def set_uplink(name: str, level: str) -> str:
    """Set a project's uplink level; returns the level set. Raises on a bad level."""
    n, level = _norm(name), _norm(level)
    if not n:
        raise ValueError("project name required")
    if level not in _UPLINK_RANK:
        raise ValueError(f"uplink must be one of: {', '.join(UPLINK_LEVELS)}")
    reg = _registry()
    e = reg.get(n) or {"uplink": "", "peers": []}
    e["uplink"] = level
    reg[n] = e
    _save_registry(reg)
    return level


def set_default_uplink(level: str) -> str:
    """Set the workspace-wide default uplink for unlisted projects."""
    level = _norm(level)
    if level not in _UPLINK_RANK:
        raise ValueError(f"uplink must be one of: {', '.join(UPLINK_LEVELS)}")
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg["default_uplink"] = level
    p.write_text(json.dumps(cfg, indent=2))
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return level


def peers_of(name: Any) -> list[str]:
    """Projects `name` is explicitly linked to on the local bus."""
    e = _registry().get(project_key(name))
    return list(e.get("peers") or []) if e else []


def add_peer(name: str, peer: str) -> list[str]:
    """Link two projects so they may see each other on the bus (symmetric)."""
    n, p = _norm(name), _norm(peer)
    if not n or not p or n == p:
        return peers_of(name)
    reg = _registry()
    for a, b in ((n, p), (p, n)):  # symmetric — link both ways
        e = reg.get(a) or {"uplink": "", "peers": []}
        e["peers"] = sorted(set(e.get("peers") or []) | {b})
        reg[a] = e
    _save_registry(reg)
    return peers_of(n)


def remove_peer(name: str, peer: str) -> list[str]:
    """Unlink two projects (symmetric)."""
    n, p = _norm(name), _norm(peer)
    reg = _registry()
    for a, b in ((n, p), (p, n)):
        e = reg.get(a)
        if e:
            e["peers"] = [x for x in (e.get("peers") or []) if x != b]
            reg[a] = e
    _save_registry(reg)
    return peers_of(n)


def peers_visible(name: Any) -> set[str]:
    """The set of projects an agent in `name` may see on the bus: itself + its
    linked peers. Used to enforce cross-project isolation. Takes a name or a
    path, like every other lookup here.
    """
    n = project_key(name)
    return {n} | set(peers_of(n)) if n else set()


# --------------------------------------------------------------------------- #
# Folders -> projects
#
# By default a project IS a git repo and its name is the repo's directory name
# (see context.git_project). That guess is right most of the time and wrong in
# the two cases that matter: a repo whose directory is called something else
# (`paas-infra` when everyone says "paas"), and a folder you want treated as its
# own project even though git says it belongs to a bigger one (one package of a
# monorepo). Registering a root pins the answer instead of guessing it, for
# every session that ever opens in that folder — hooks, MCP server, and CLI all
# read the same registry.
# --------------------------------------------------------------------------- #
def roots_of(name: Any) -> list[str]:
    """The folders explicitly registered as belonging to `name`."""
    e = _registry().get(_norm(name))
    return list(e.get("roots") or []) if e else []


def register_root(name: str, root: str, *, uplink: str | None = None) -> tuple[str, str]:
    """Bind a folder to a project name. Returns (name, stored root).

    A root belongs to exactly one project: re-registering it under a new name
    moves it, rather than leaving the same directory claimed twice (whichever
    entry the dict happened to yield first would win, which is not a thing to
    debug at 3am).
    """
    n, r = _norm(name), _norm_root(root)
    if not n:
        raise ValueError("project name required")
    if not r:
        raise ValueError("folder required")
    if uplink is not None and _norm(uplink) not in _UPLINK_RANK:
        raise ValueError(f"uplink must be one of: {', '.join(UPLINK_LEVELS)}")
    reg = _registry()
    for other, e in reg.items():  # move, don't duplicate
        if other != n:
            e["roots"] = [x for x in (e.get("roots") or []) if x != r]
    e = reg.get(n) or {"uplink": "", "peers": [], "roots": []}
    e["roots"] = sorted(set(e.get("roots") or []) | {r})
    if uplink is not None:
        e["uplink"] = _norm(uplink)
    reg[n] = e
    _save_registry(reg)
    return n, r


def unregister_root(root: str) -> str | None:
    """Forget a folder. Returns the project it belonged to, or None. The project
    entry itself stays: its uplink and peer links are settings, not consequences
    of one directory existing.
    """
    r = _norm_root(root)
    reg = _registry()
    owner = None
    for name, e in reg.items():
        if r in (e.get("roots") or []):
            owner = name
            e["roots"] = [x for x in e["roots"] if x != r]
    if owner:
        _save_registry(reg)
    return owner


def project_for_path(path: Any) -> tuple[str, str] | None:
    """(project name, registered root) for the deepest registered folder that
    contains `path` — or None. Deepest wins so a package registered inside an
    already-registered monorepo keeps its own identity.
    """
    p = _norm_root(path)
    if not p:
        return None
    best: tuple[str, str] | None = None
    for name, e in _registry().items():
        for r in e.get("roots") or []:
            if p == r or p.startswith(r.rstrip(os.sep) + os.sep):
                if best is None or len(r) > len(best[1]):
                    best = (name, r)
    return best


def message_project(topic: Any, project: Any = None) -> str | None:
    """The project a bus message belongs to: its stored project, else the name
    embedded in a `proj:<name>` topic, else None (a cross-cutting channel).

    Always a registry key — the stored `project` is a path, and returning it raw
    is what made the message dial a no-op.
    """
    if project:
        return project_key(project)
    t = str(topic or "")
    if t.startswith("proj:"):
        return _norm(t[5:])
    return None


# ---- Backward-compatible content-allowlist shims (used by capture_turns and the
# `cabildo share` shortcut, now expressed as uplink=='content'). --------------- #
def shared_projects() -> list[str]:
    """Projects whose uplink is 'content' — cleared to push conversation turns."""
    return [n for n, e in _registry().items() if _norm(e.get("uplink")) == "content"]


def add_shared_project(project: str) -> list[str]:
    """`cabildo share <p>` — raise a project to content uplink."""
    set_uplink(project, "content")
    return shared_projects()


def remove_shared_project(project: str) -> list[str]:
    """`cabildo share <p> --remove` — drop content back to the default uplink."""
    set_uplink(project, default_uplink())
    return shared_projects()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _api(base: str, token: str, method: str, path: str, body=None) -> dict | list:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# The mirror — active sessions, new messages (incremental), live claims.
# --------------------------------------------------------------------------- #
def _cursor_file() -> Path:
    return store.db_path().parent / "cloud_sync.json"


def _load_cursor() -> dict:
    try:
        return json.loads(_cursor_file().read_text())
    except Exception:
        return {"msg": 0, "out": 0}


def _save_cursor(cur: dict) -> None:
    try:
        _cursor_file().write_text(json.dumps(cur))
    except Exception:
        pass


def sync_once(base: str, token: str) -> dict:
    """One pass: push active sessions, any new messages since the cursor, and
    live claims. Reads the bus read-only; the cursor keeps message sync
    incremental and restart-safe. Returns a small counts dict.
    """
    dbp = store.db_path()
    if not dbp.exists():
        return {"ok": False, "error": f"no local bus at {dbp}"}
    import sqlite3

    cur = _load_cursor()
    now = time.time()
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Presence (uplink>=presence): a project's agents show up on the
        # dashboard. Isolated projects (uplink=none) never appear at all.
        agents = conn.execute(
            "SELECT session_id, handle, project_name, cwd FROM sessions "
            "WHERE status='active' AND last_seen_at>=?",
            (now - 1800,),
        ).fetchall()
        agents = [a for a in agents if uplink_allows(a["project_name"], "presence")]
        for a in agents:
            _api(
                base,
                token,
                "POST",
                "/api/agents",
                {
                    "session_id": a["session_id"],
                    "handle": a["handle"],
                    "project": a["project_name"],
                    "cwd": a["cwd"],
                },
            )

        # Message bodies (uplink>=messages). Anything the downlink injected is
        # already in the cloud; mirroring it back up would duplicate it forever.
        has_origin = any(r["name"] == "origin" for r in conn.execute("PRAGMA table_info(messages)"))
        has_proj = any(r["name"] == "project" for r in conn.execute("PRAGMA table_info(messages)"))
        # Skip anything not born here: 'cloud' rows came down the downlink (echoing
        # them up duplicates forever), and 'lan'/'wan' rows came from a peer over a
        # local or tunnelled transport — those must never silently escape to the
        # hosted mirror (that is the point of the local-only transports in
        # lan.py / wan.py).
        not_echo = (
            " AND COALESCE(origin,'local') NOT IN ('cloud','lan','wan')" if has_origin else ""
        )
        proj_col = ", project" if has_proj else ""
        msgs = conn.execute(
            f"SELECT id, from_session, from_handle, topic, to_session, body, urgent"
            f"{proj_col} FROM messages WHERE id>?{not_echo} ORDER BY id LIMIT 300",
            (cur.get("msg", 0),),
        ).fetchall()
        pushed_msgs = 0
        for m in msgs:
            cur["msg"] = m["id"]  # advance regardless — skipped ones don't retry
            mp = message_project(m["topic"], m["project"] if has_proj else None)
            if not uplink_allows(mp, "messages"):
                continue
            _api(
                base,
                token,
                "POST",
                "/api/messages",
                {
                    "from_session": m["from_session"],
                    "from_handle": m["from_handle"],
                    "topic": m["topic"],
                    "to_session": m["to_session"],
                    "body": m["body"],
                    "urgent": bool(m["urgent"]),
                },
            )
            pushed_msgs += 1

        # Locks (uplink>=presence): a coordination signal, not content.
        claims = conn.execute(
            "SELECT resource, holder_session, holder_handle, project, note, expires_at "
            "FROM claims WHERE expires_at>?",
            (now,),
        ).fetchall()
        claims = [c for c in claims if uplink_allows(c["project"], "presence")]
        for c in claims:
            _api(
                base,
                token,
                "POST",
                "/api/claims",
                {
                    "resource": c["resource"],
                    "holder_session": c["holder_session"],
                    "holder_handle": c["holder_handle"],
                    "project": c["project"],
                    "note": c["note"],
                    "ttl_seconds": max(1, c["expires_at"] - now),
                },
            )

        # Conversation capture (uplink==content) — only opted-in projects.
        turns_pushed = capture_turns(base, token, cur, agents)
    finally:
        conn.close()

    # Cost telemetry is derived from a full transcript scan, so throttle it to
    # once every COST_EVERY seconds regardless of how often we mirror the bus.
    cost_pushed = False
    if now - cur.get("cost_at", 0) >= COST_EVERY:
        try:
            _api(base, token, "POST", "/api/cost", cost_snapshot())
            cur["cost_at"] = now
            cost_pushed = True
        except Exception:
            pass

    _save_cursor(cur)
    return {
        "ok": True,
        "agents": len(agents),
        "messages": pushed_msgs,
        "claims": len(claims),
        "cost": cost_pushed,
        "turns": turns_pushed,
    }


def drain_once(base: str, token: str) -> dict:
    """The downlink: pull messages composed in the hosted dashboard into the
    local bus, so a reply typed in the browser reaches the agent it addresses.

    Rows land with origin='cloud' and `sync_once` skips those, which is what
    stops the mirror from bouncing them back up as duplicates.
    """
    dbp = store.db_path()
    if not dbp.exists():
        return {"ok": False, "error": f"no local bus at {dbp}"}
    cur = _load_cursor()
    msgs = _api(base, token, "GET", f"/api/messages/outbound?since={cur.get('out', 0)}")
    if not isinstance(msgs, list):
        return {"ok": False, "error": str(msgs)[:200]}
    conn = store.connect()  # opening through store runs the migration
    n = skipped = 0
    try:
        for m in msgs:
            topic, to_session = m.get("topic"), m.get("to_session")
            cur["out"] = max(cur.get("out", 0), m.get("created") or 0)
            if bool(topic) == bool(to_session):
                skipped += 1  # exactly one of the two must be set
                continue
            project = topic[5:] if topic and topic.startswith("proj:") else None
            store.post_message(
                conn,
                from_session=m.get("from_session") or "human:cloud",
                from_handle=m.get("from_handle"),
                body=m.get("body") or "",
                topic=topic,
                to_session=to_session,
                project=project,
                urgent=bool(m.get("urgent")),
                origin="cloud",
            )
            n += 1
    finally:
        conn.close()
    _save_cursor(cur)
    return {"ok": True, "delivered": n, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Conversation capture — ship allowlisted sessions' prompts + replies up.
#
# This is the private half of a session: what the human typed and what Claude
# said back, distinct from the inter-agent bus messages. Only projects on the
# allowlist are touched (see shared_projects). Transcripts are append-only, so a
# per-session byte offset in cur["turns"] makes capture incremental and
# restart-safe — we ship only what's new since last pass.
# --------------------------------------------------------------------------- #
def _transcript_path(root, session_id: str, cwd: str | None):
    """Locate a session's transcript file. Claude names the directory after the
    cwd (project_slug), so the precise path is root/<slug>/<sid>.jsonl; if the
    session ran from a different cwd than we recorded, fall back to a recursive
    glob for the id.
    """
    from .transcript import project_slug

    if cwd:
        precise = root / project_slug(cwd) / f"{session_id}.jsonl"
        if precise.exists():
            return precise
    hits = list(root.glob(f"**/{session_id}.jsonl"))
    return hits[0] if hits else None


def capture_turns(base: str, token: str, cur: dict, agents, log=None) -> int:
    """For each allowlisted active session, push conversation turns new since the
    last byte offset and advance that offset. `agents` is the same session rows
    sync_once already fetched (session_id, handle, project_name, cwd). Returns
    the number of sessions that shipped at least one new turn.

    Offsets live in cur["turns"][session_id]; on a push failure we leave the
    offset unadvanced so the turns retry next pass rather than being lost.
    """
    from . import transcript

    allow = set(shared_projects())
    if not allow:
        return 0
    root = transcript.transcript_root()
    offsets = cur.setdefault("turns", {})
    pushed = 0
    for a in agents:
        project = (a["project_name"] or "").strip().lower()
        if project not in allow:
            continue
        sid = a["session_id"]
        path = _transcript_path(root, sid, a["cwd"])
        if path is None:
            continue
        after = int(offsets.get(sid, 0))
        turns, new_off = transcript.session_turns(path, after_bytes=after)
        if not turns:
            offsets[sid] = new_off  # nothing new, but keep any rotation reset
            continue
        resp = _api(
            base,
            token,
            "POST",
            "/api/turns",
            {
                "session_id": sid,
                "handle": a["handle"],
                "project": a["project_name"],
                "cwd": a["cwd"],
                "turns": turns,
            },
        )
        if isinstance(resp, dict) and resp.get("ok") is False:
            if log:
                log(f"turns push failed for {sid[:8]}: {resp.get('error')}")
            continue  # leave offset unadvanced — retry next pass
        offsets[sid] = new_off
        pushed += 1
    return pushed


COST_EVERY = 300  # seconds — recompute the (transcript-scanning) cost at most this often


def cost_snapshot() -> dict:
    """A compact version of `cabildo cost` for the hosted dashboard: context this
    machine injects into sessions, hook latency per event, and the tokens agents
    spend calling cabildo tools. Char->token is the same rough /4 as the CLI.
    """
    import socket

    from . import transcript
    from .transcript import CHARS_PER_TOKEN

    conn = store.connect()
    m = store.hook_metrics_summary(conn)
    tr = transcript.scan()
    t = m["total"]
    injected_tok = (t["chars"] or 0) // CHARS_PER_TOKEN
    return {
        "machine": socket.gethostname(),
        "injected": {
            "chars": t["chars"] or 0,
            "tokens": injected_tok,
            "fires": t["fires"] or 0,
            "sessions": t["sessions"] or 0,
        },
        "hooks": [
            {
                "event": e["event"],
                "fires": e["fires"],
                "injections": e["injections"],
                "chars": e["chars"],
                "avg_ms": round(e["avg_ms"] or 0, 1),
                "total_s": round((e["total_ms"] or 0) / 1000, 1),
            }
            for e in m["per_event"]
        ],
        "traffic": {
            "calls": tr["calls"],
            "files": tr["files"],
            "in_tok": tr["input_chars"] // CHARS_PER_TOKEN,
            "out_tok": tr["output_chars"] // CHARS_PER_TOKEN,
            "tokens_est": tr["tokens_est"],
            "by_tool": sorted(
                [
                    {
                        "tool": name.replace("mcp__cabildo__", ""),
                        "calls": e["calls"],
                        "in_tok": e["input_chars"] // CHARS_PER_TOKEN,
                        "out_tok": e["output_chars"] // CHARS_PER_TOKEN,
                    }
                    for name, e in tr["by_tool"].items()
                ],
                key=lambda x: -(x["in_tok"] + x["out_tok"]),
            )[:20],
        },
        "total_tok": injected_tok + tr["tokens_est"],
    }
