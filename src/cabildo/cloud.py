"""The local project registry and per-project visibility dials.

`cloud.json` under ~/.cabildo is the writable store for this: which folders
belong to which project, which projects may see each other on the local bus
(peers), and a per-project `uplink` dial. Everything here is local: nothing in
this module talks to the network. The `uplink` dial is currently inert (no
transport reads it) but is kept in place so a later redesign can revisit it.

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Config — cloud.json under ~/.cabildo, a 600-mode json file that doubles as the
# writable store for the project registry below.
# --------------------------------------------------------------------------- #
def config_path() -> Path:
    """``cloud.json`` in ``CABILDO_HOME`` (default ``~/.cabildo``), like the bus DB."""
    home = os.environ.get("CABILDO_HOME")
    return Path(home or os.path.expanduser("~/.cabildo")) / "cloud.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except Exception:
        return {}


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


# Short TTL on the per-project [link] config read, so peers_visible — called on
# every gated bus op — doesn't re-read the same TOML files dozens of times a
# second. Same cheap-cache shape as project_key's.
_LINK_TTL = 10.0
_link_cache: dict[str, tuple[float, frozenset[str], bool]] = {}


def _link_cfg(name: str) -> tuple[frozenset[str], bool]:
    """(friends, open) declared by project `name` in its repo-local
    `.cabildo/config.toml` (read via its registered root) merged with the global
    `[project.<name>]` section. Empty/False when it declares nothing or its root
    is unknown. Cached briefly — config is advisory and this is a hot path.
    """
    now = time.time()
    hit = _link_cache.get(name)
    if hit and now - hit[0] < _LINK_TTL:
        return hit[1], hit[2]
    friends: frozenset[str] = frozenset()
    is_open = False
    try:
        from . import config

        roots = roots_of(name)
        cwd = roots[0] if roots else None
        friends = frozenset(config.link_friends(cwd, name))
        is_open = config.link_open(cwd, name)
    except Exception:
        pass
    _link_cache[name] = (now, friends, is_open)
    return friends, is_open


def peers_visible(name: Any) -> set[str]:
    """The set of projects an agent in `name` may see and talk to on the bus:
    itself, plus every project it is linked to. Used to enforce cross-project
    isolation. Takes a name or a path, like every other lookup here.

    Three ways a project B joins A's set, isolation-preserving by construction:
      * a legacy symmetric `cabildo project --link` peer (cloud.json);
      * a MUTUAL config friendship — A names B in `[link].friends` AND B names A
        back (so no project can unilaterally join another's bus by editing only
        its own file); `[link].open = true` is "friends = everyone", satisfying a
        project's own half of every link;
      * B is open — an open project accepts every project, so anyone may reach it.

    Config friends are resolved across the registered projects, so an open or
    befriending project must be known to the registry (via `cabildo add`) or
    declared in the global `[project.<name>]` section to be reachable.
    """
    n = project_key(name)
    if not n:
        return set()
    visible = {n} | set(peers_of(n))  # legacy cloud.json symmetric links
    my_friends, my_open = _link_cfg(n)
    for b in set(project_registry()) | visible:
        if b == n:
            continue
        b_friends, b_open = _link_cfg(b)
        a_lists_b = my_open or b in my_friends
        b_lists_a = b_open or n in b_friends
        if b_open or (a_lists_b and b_lists_a):
            visible.add(b)
    return visible


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


# ---- Content-allowlist helpers: the projects raised to uplink=='content'.
# Inert for now (no transport reads the uplink dial); kept for the redesign. ---- #
def shared_projects() -> list[str]:
    """Projects whose uplink is 'content'."""
    return [n for n, e in _registry().items() if _norm(e.get("uplink")) == "content"]


def add_shared_project(project: str) -> list[str]:
    """Raise a project to content uplink."""
    set_uplink(project, "content")
    return shared_projects()


def remove_shared_project(project: str) -> list[str]:
    """Drop a project's content uplink back to the default."""
    set_uplink(project, default_uplink())
    return shared_projects()
