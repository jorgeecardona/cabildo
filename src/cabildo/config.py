"""config.py: layered, read-only TOML settings.

Two optional files, both human-edited. cabildo never writes them: the package
is stdlib-only at runtime and the stdlib has a TOML reader (``tomllib``) but no
writer, so the JSON registry in ``cloud.json`` stays the one writable store.
This layer is for the dials a human sets by hand and wants to travel with a
repo.

Precedence, highest first:

    <tree_root>/.cabildo/config.toml   repo-local, beside the push gate
                                       (.cabildo/gate.sh); commit it to share a
                                       policy with everyone on the checkout
    ~/.cabildo/config.toml             this machine, all projects; a
                                       [project.<name>] section scopes keys to
                                       one repo
    {}                                 built-in defaults (each caller's own)

Merging is table by table, so a repo may set just ``[push]`` and inherit
``[watch]`` from the global file. Within the global file, a ``[project.<name>]``
section overrides the top-level tables for that one project.

Nothing here raises: a missing file is empty, and a malformed file is skipped
rather than taking down the guard or the CLI that reads it.

Recognised tables (``push``, ``destructive`` and ``watch`` are wired to behaviour
today; the rest are read generically and wired one call site at a time):

    [project] name = "..."                   # repo-local identity pin (git_project)
    [project.<name>] isolated = true         # global: catch-all gets no channel
    [grants]  approver = "human" | "project"  # who decides cross-project requests
    [link]    friends = [...], open = true    # mutual bus visibility (cloud.peers_visible)
    [push]        mode = "auto" | "manual"     # guard._deny_push
    [destructive] mode = "guard" | "off"        # guard._deny_destructive_git
    [trunk]   mode = "off" | "guard", names   # guard._deny_commit_on_trunk (opt-in)
    [watch]   topic, interval, tail         # cli.cmd_watch
    [uplink]  level = "none" | ...          # cloud
    [notify]  enabled, quiet_hours          # desktop / notify
    [push]    target, local_sync, cleanup_lanes   # actions.push (#20, #24)
    [spawn]   model, max_agents, max_depth   # request spawn
    [record]  messages = true                # config.record_messages (daemon ingest)
    [roles]   <name> = "responsibilities"    # config.roles (agent-card roster)
    [project.<name>.remote]  peers, token, bind, interval   # wan.py, global file only
    [browser.login.<site>]   origin, username, password, otp   # browser/credentials.py, global only
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from . import store
from .context import git_project, tree_root

# Value the [watch] table can use to mean "this repo's own channel", resolved to
# proj:<name> at read time rather than hard-coding the name in every checkout.
WATCH_TOPIC_PROJECT = "project"


def _global_path() -> Path:
    """The machine-wide config, next to the bus DB and honouring CABILDO_HOME."""
    return store.home() / "config.toml"


def _read(path: Path) -> dict[str, Any]:
    """Parse a TOML file into a dict. Missing or malformed reads as ``{}`` --
    config is advisory, and a typo in it must never break the tool that reads it.
    """
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (
        FileNotFoundError,
        NotADirectoryError,
        PermissionError,
        tomllib.TOMLDecodeError,
        OSError,
    ):
        return {}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> None:
    """Merge ``over`` into ``base`` in place: nested tables merge, leaves in
    ``over`` win. So a repo's [push] and a global [watch] coexist.
    """
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load(cwd: str | None = None, project_name: str | None = None) -> dict[str, Any]:
    """The merged settings for the project containing ``cwd``.

    ``project_name`` may be passed to skip the git lookup (the guard already
    knows it); otherwise it is derived from ``cwd``. The ``[project]`` table of
    the global file is scoping, not a setting, so it never appears in the result.
    """
    glob = _read(_global_path())
    projects = glob.get("project")
    projects = projects if isinstance(projects, dict) else {}
    global_top = {k: v for k, v in glob.items() if k != "project"}

    if project_name is None:
        try:
            _, project_name = git_project(cwd)
        except Exception:
            project_name = None

    merged: dict[str, Any] = {}
    _deep_merge(merged, global_top)  # lowest precedence
    if project_name:
        section = projects.get(project_name.lower())
        if isinstance(section, dict):
            _deep_merge(merged, section)
    _deep_merge(merged, _read(Path(tree_root(cwd)) / ".cabildo" / "config.toml"))  # highest
    return merged


def repo_project_name(tree_root: str | None) -> str | None:
    """``[project].name`` from the repo-local ``.cabildo/config.toml`` at
    ``tree_root``, lowered, or None.

    This is the repo's committed statement of its own identity, pinning the
    project name against the directory-basename guess (issue #26, leak #1: two
    repos sharing a folder name must not share a channel). It is read WITHOUT a
    git lookup -- a plain file read of one known path -- so ``git_project`` can
    consult it without recursing back through ``load()``, which itself calls
    ``git_project``. A missing or malformed file reads as "no pin".
    """
    if not tree_root:
        return None
    proj = _read(Path(tree_root) / ".cabildo" / "config.toml").get("project")
    if isinstance(proj, dict):
        name = proj.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip().lower()
    return None


def chat_room() -> str | None:
    """The chatroom every session joins at start: ``[chat] room`` in the machine-wide
    config, ``general`` by default. An empty string turns the auto-join off.
    """
    t = _read(_global_path()).get("chat")
    name = t.get("room", store.DEFAULT_TOPIC) if isinstance(t, dict) else store.DEFAULT_TOPIC
    return str(name or "").lstrip("#").strip() or None


def share_general() -> bool:
    """``[chat] share_general``: whether #general posts go to paired machines. Default False."""
    t = _read(_global_path()).get("chat")
    val = t.get("share_general", False) if isinstance(t, dict) else False
    return val is True or str(val).strip().lower() in ("1", "true", "yes", "on")


def project_isolated_setting(
    cwd: str | None = None, project_name: str | None = None
) -> bool | None:
    """``[project.<name>].isolated`` when the config says it, else None (not set).

    False is a choice too: it keeps a shared channel for a folder cabildo would otherwise treat
    as a catch-all (see ``context.is_catch_all``).
    """
    val = get(cwd, "isolated", project_name=project_name, default=None)
    if val is None:
        return None
    return val is True or str(val).strip().lower() in ("1", "true", "yes", "on")


def project_isolated(cwd: str | None = None, project_name: str | None = None) -> bool:
    """``[project.<name>].isolated`` for this project: default False.

    True marks a catch-all name (``self``, ``tmp``, a home dir) that must NOT get
    a shared ``proj:<name>`` channel -- agents there are isolated, DM-only (issue
    #26, leak #2: parent directories pooling unrelated agents into one room). Set
    in the machine-wide ``~/.cabildo/config.toml`` under ``[project.<name>]``.
    """
    val = get(cwd, "isolated", project_name=project_name, default=False)
    return val is True or str(val).strip().lower() in ("1", "true", "yes", "on")


def get(
    cwd: str | None = None,
    *path: str,
    default: Any = None,
    project_name: str | None = None,
) -> Any:
    """Walk the merged config by ``path`` (e.g. ``get(cwd, "push", "mode")``),
    returning ``default`` if any hop is missing or not a table.
    """
    node: Any = load(cwd, project_name)
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def push_mode(cwd: str | None = None, project_name: str | None = None) -> str:
    """``[push].mode`` for this project: "manual" or the default "auto".

    An unknown value falls back to "auto" -- the safe policy, since "manual"
    turns the push guard off.
    """
    mode = str(get(cwd, "push", "mode", project_name=project_name, default="auto")).lower()
    return mode if mode in ("auto", "manual") else "auto"


def push_verify(cwd: str | None = None, project_name: str | None = None) -> bool:
    """``[push].verify``: whether the daemon's push push re-runs the repo's
    pre-push hook. Default False -- the daemon already ran ``.cabildo/gate.sh``
    immediately before, so re-verifying inside the push duplicates that work and,
    with a slow pre-push, times the push out (issue #14). Set true to keep the
    hook running on the daemon's push.
    """
    val = get(cwd, "push", "verify", project_name=project_name, default=False)
    return val is True or str(val).strip().lower() in ("1", "true", "yes", "on")


def push_rate(cwd: str | None = None, project_name: str | None = None) -> tuple[int, float]:
    """``[push].max_per_window`` pushes one session may land on one target within
    ``[push].window_minutes`` (defaults 10 and 30), as ``(count, seconds)``. A count of 0 turns
    the limit off. It stops a ``/loop`` that queues a push every iteration (#39).
    """

    def number(key: str, default: float) -> float:
        try:
            return float(get(cwd, "push", key, project_name=project_name, default=default))
        except (TypeError, ValueError):
            return default

    return int(number("max_per_window", 10)), max(1.0, number("window_minutes", 30.0)) * 60.0


def push_timeout(cwd: str | None = None, project_name: str | None = None) -> float:
    """``[push].timeout_s``: seconds the push push may take before the daemon
    gives up. Default 300 -- git's plain 120 guillotines a slow network or a
    legitimately large push (issue #14).
    """
    val = get(cwd, "push", "timeout_s", project_name=project_name, default=300.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 300.0


def ci_poll(cwd: str | None = None) -> float:
    """``[ci].poll_s``: seconds between GitLab pipeline polls by the daemon.

    Every failed pipeline of a repo with an active session is reported: a DM to
    the agent that pushed the commit, else an urgent post to the project
    channel. Default 60. 0 turns the poller off.
    """
    val = get(cwd, "ci", "poll_s", default=60.0)
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return 60.0


def push_ci_watch(cwd: str | None = None, project_name: str | None = None) -> float:
    """``[push].ci_watch_s``: seconds the daemon waits for the GitLab pipeline of a pushed commit.

    On a failed pipeline the daemon DMs the pusher the tail of each failed job log. Default
    1800. 0 turns the watch off.
    """
    val = get(cwd, "push", "ci_watch_s", project_name=project_name, default=1800.0)
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return 1800.0


def push_local_sync(cwd: str | None = None, project_name: str | None = None) -> str:
    """``[push].local_sync`` for this project: how a successful push updates the
    pusher's LOCAL ``main`` (issue #20).

    The daemon pushes to ``origin/main`` but deliberately never touches a working
    checkout's branch mid-edit, so without this local ``main`` drifts further
    behind ``origin/main`` after every push, and agents reach for
    ``git reset --hard`` (which the guard denies).

    - ``"off"`` -- no local git op; today's behavior.
    - ``"ff-only"`` (the default) -- fast-forward local ``main`` to
      ``origin/main`` after the push, and REFUSE (warn, never clobber) when it
      cannot: a diverged local ``main``, or ``main`` checked out in a worktree.
      A refusal never fails the push.
    - ``"reset"`` -- hard-reset local ``main`` to ``origin/main``. Destructive;
      strictly opt-in.

    An unknown value falls back to "ff-only", the safe-by-construction policy.
    """
    mode = str(get(cwd, "push", "local_sync", project_name=project_name, default="ff-only")).lower()
    return mode if mode in ("off", "ff-only", "reset") else "ff-only"


def push_cleanup_lanes(cwd: str | None = None, project_name: str | None = None) -> str:
    """``[push].cleanup_lanes`` for this project (issue #24): what the daemon does
    with a throwaway worktree after a verified-successful push.

    - ``"off"`` (the default) -- leave every worktree in place; today's behavior.
    - ``"merged"`` -- remove the worktree IFF it is a linked worktree with no
      uncommitted tracked changes whose HEAD is already merged into the remote
      target (nothing to lose). Never touches a main checkout or a worktree with
      unpushed work.

    Default off on purpose: cabildo is installed editable, so this runs in the
    live daemon every agent pushes through; a repo opts in once it trusts it.
    An unknown value falls back to ``"off"``.
    """
    mode = str(get(cwd, "push", "cleanup_lanes", project_name=project_name, default="off")).lower()
    return mode if mode in ("off", "merged") else "off"


def destructive_mode(cwd: str | None = None, project_name: str | None = None) -> str:
    """``[destructive].mode`` for this project: "off" or the default "guard".

    "guard" (the default) denies tree-destroying git ops (`reset --hard`, a
    discarding `checkout`/`restore`, `clean -f`, `stash`) whether or not another
    agent shares the tree -- an autonomous agent should not throw away work it
    cannot bring back. "off" turns the guard off for repos that want it. An
    unknown value falls back to "guard", the safe policy.
    """
    mode = str(get(cwd, "destructive", "mode", project_name=project_name, default="guard")).lower()
    return mode if mode in ("guard", "off") else "guard"


def spend_mode(cwd: str | None = None, project_name: str | None = None) -> str:
    """``[workspace].spend``: when the mode line shows consumed tokens and cost.

    "auto" (the default) shows it on an API-key login, where there is no subscription
    limit to show instead, and hides it on a subscription. "always" shows it either way,
    which is also how someone on a subscription sees what their work would have cost.
    "never" hides it. An unknown value falls back to "auto".
    """
    mode = str(get(cwd, "workspace", "spend", project_name=project_name, default="auto")).lower()
    return mode if mode in ("auto", "always", "never") else "auto"


def trunk_mode(cwd: str | None = None, project_name: str | None = None) -> str:
    """``[trunk].mode`` for this project: "off" (the default) or "guard".

    "guard" denies an agent committing directly on a protected trunk branch
    (``main``/``master``, or ``[trunk].names``). The daemon advances
    ``origin/main`` from lanes and cannot fast-forward a checkout that carries
    its own commits, so a commit made straight on local ``main`` diverges it
    from origin and drifts one commit further per push to origin until ff can no
    longer reconcile it. Work then belongs on a lane pushed to origin via
    ``request push``; ``main`` stays a ref the daemon keeps current.

    It is "off" by default: a plain commit on ``main`` is the ordinary flow for a
    trunk-based solo repo, and the guard only earns its cost where the daemon is
    actually advancing ``origin/main`` from lanes under concurrent agents — a
    repo opts in with ``[trunk].mode = "guard"`` when it runs that way. An
    unknown value falls back to "off".
    """
    mode = str(get(cwd, "trunk", "mode", project_name=project_name, default="off")).lower()
    return mode if mode in ("guard", "off") else "off"


def trunk_names(cwd: str | None = None, project_name: str | None = None) -> set[str]:
    """``[trunk].names``: the branch names treated as protected trunk by
    ``trunk_mode``. Default ``{"main", "master"}``; a repo whose trunk has
    another name overrides it (e.g. ``names = ["trunk"]``). A non-list value, or
    one that is empty after cleaning, falls back to the default.
    """
    node = get(cwd, "trunk", "names", project_name=project_name, default=None)
    if isinstance(node, (list, tuple)):
        names = {str(n).strip() for n in node if str(n).strip()}
        if names:
            return names
    return {"main", "master"}


def roles(cwd: str | None = None, project_name: str | None = None) -> dict[str, str]:
    """``[roles]`` for this project: the configured roster, a mapping of role
    name -> responsibilities text. Agents may ``set_role`` to a name in here (and
    inherit its responsibilities) or to a freehand string not listed here. Empty
    by default; a project defines its cabinet by adding the table.

    Names are normalized to lowercase so lookups are case-insensitive; a
    non-table ``[roles]`` value, or a non-string entry, is ignored rather than
    raising into the caller.
    """
    node = get(cwd, "roles", project_name=project_name, default={})
    if not isinstance(node, dict):
        return {}
    return {
        str(name).strip().lower(): str(text) for name, text in node.items() if isinstance(text, str)
    }


def grants_approver(cwd: str | None = None, project_name: str | None = None) -> str:
    """``[grants].approver`` for this project: who decides a cross-project access
    request aimed at it (issue #26).

    - ``"human"`` (the default) -- the request is put to the human; nothing
      connects without them.
    - ``"project"`` -- any active agent in this project may decide, enabling
      autonomous cross-project coordination.

    An unknown value falls back to ``"human"``, the conservative policy.
    """
    val = str(get(cwd, "grants", "approver", project_name=project_name, default="human")).lower()
    return val if val in ("human", "project") else "human"


def record_messages(cwd: str | None = None, project_name: str | None = None) -> bool:
    """``[record].messages`` for this project: default False.

    True opts this repo's conversation turns into cabildo's own DB (the daemon,
    started with --save-messages, ingests its transcripts). Off by default and
    per-project on purpose: a repo whose transcripts must never enter the shared
    bus simply never sets it, and nothing is recorded for it.
    """
    val = get(cwd, "record", "messages", project_name=project_name, default=False)
    return val is True or str(val).strip().lower() in ("1", "true", "yes", "on")


def timeline_interval(cwd: str | None = None, project_name: str | None = None) -> float:
    """``[timeline].interval_s``: minimum seconds between wall-clock timeline
    lines injected into a session (see notify.timeline_text). Default 600 (ten
    minutes). ``0`` or a negative value turns the timeline off.

    Hooks have no timer, so this is a floor, not a schedule: the line rides the
    next hook event (a tool call mid-turn, the prompt on an idle session), and an
    interval passing while a session sits idle just means the next event carries a
    slightly older-than-``interval`` gap.
    """
    val = get(cwd, "timeline", "interval_s", project_name=project_name, default=600.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 600.0


def timeline_heartbeat(cwd: str | None = None, project_name: str | None = None) -> float:
    """``[timeline].heartbeat_s``: if > 0, the Stop hook fires a one-shot heartbeat
    nudge (see notify.heartbeat_reason) when this many seconds have passed since the
    session's last one, forcing one more turn before it idles. Default 0 (off).

    Bounded by design: the Stop hook honours ``stop_hook_active``, so the nudge can
    only ever buy a single extra turn, never a runaway loop. It cannot wake a
    session that has already stopped — no hook can — only defer an imminent idle.
    """
    val = get(cwd, "timeline", "heartbeat_s", project_name=project_name, default=0.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def link_friends(cwd: str | None = None, project_name: str | None = None) -> set[str]:
    """``[link].friends`` for this project: the other projects it names as friends,
    lowercased. Links are MUTUAL — this list is only half of a link; the other
    project must name this one back (or be ``[link].open``) before the two can see
    or message each other on the bus (see cloud.peers_visible). Empty by default,
    so a project stays isolated until it opts in. A non-list value reads as empty.
    """
    node = get(cwd, "link", "friends", project_name=project_name, default=None)
    if isinstance(node, (list, tuple)):
        return {str(n).strip().lower() for n in node if str(n).strip()}
    return set()


def link_open(cwd: str | None = None, project_name: str | None = None) -> bool:
    """``[link].open`` for this project: default False.

    True means "friends = everyone" — the project accepts every other project on
    the bus without naming them, for a shared/open project others may freely reach.
    It still composes with the mutual rule: an open project satisfies its own half
    of every link automatically, so anyone who names it (or is itself open) links.
    """
    val = get(cwd, "link", "open", project_name=project_name, default=False)
    return val is True or str(val).strip().lower() in ("1", "true", "yes", "on")


def watch_topic(cwd: str | None = None, project_name: str | None = None) -> str | None:
    """``[watch].topic`` for this project, resolving the ``"project"`` sentinel
    to this repo's own channel. ``None`` means "unset" -- the caller keeps its
    own default (all channels).
    """
    topic = get(cwd, "watch", "topic", project_name=project_name, default=None)
    if topic is None:
        return None
    topic = str(topic)
    if topic == WATCH_TOPIC_PROJECT:
        name = project_name
        if name is None:
            try:
                _, name = git_project(cwd)
            except Exception:
                return None
        return store.project_topic(name) if name else None
    return topic


# --------------------------------------------------------------------------- #
# [project.<name>.remote] -- cross-machine sync over a tunnel (wan.py)
# --------------------------------------------------------------------------- #
def remote_tables() -> dict[str, dict[str, Any]]:
    """``[project.<name>.remote]`` of the machine-wide config, by project name.

    The daemon reads only this file: it cannot discover repos, and a token does not
    belong in a repo's committed config.
    """
    projects = _read(_global_path()).get("project")
    if not isinstance(projects, dict):
        return {}
    return {
        name: section["remote"]
        for name, section in projects.items()
        if isinstance(section, dict) and isinstance(section.get("remote"), dict)
    }


# --------------------------------------------------------------------------- #
# [browser.login.<site>] -- 1Password references for browser_login
# --------------------------------------------------------------------------- #
def browser_login_tables() -> dict[str, Any]:
    """``[browser.login.<site>]`` of the machine-wide config, by site name.

    Only the global file is read. A repo's committed config could otherwise point a site at an
    origin it controls.
    """
    browser = _read(_global_path()).get("browser")
    login = browser.get("login") if isinstance(browser, dict) else None
    return dict(login) if isinstance(login, dict) else {}
