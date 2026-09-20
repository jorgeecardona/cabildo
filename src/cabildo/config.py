"""config.py: layered, read-only TOML settings.

Two optional files, both human-edited. cabildo never writes them: the package
is stdlib-only at runtime and the stdlib has a TOML reader (``tomllib``) but no
writer, so the JSON registry in ``cloud.json`` stays the one writable store.
This layer is for the dials a human sets by hand and wants to travel with a
repo.

Precedence, highest first:

    <tree_root>/.cabildo/config.toml   repo-local, beside the land gate
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

    [push]        mode = "land" | "manual"     # guard._deny_push
    [destructive] mode = "guard" | "off"        # guard._deny_destructive_git
    [watch]   topic, interval, tail         # cli.cmd_watch
    [uplink]  level = "none" | ...          # cloud
    [notify]  enabled, quiet_hours          # desktop / notify
    [land]    target = "main"               # actions.land
    [spawn]   model, max_agents, max_depth   # request spawn
    [record]  messages = true                # config.record_messages (daemon ingest)
    [remote]  peers, token, bind, interval   # wan.py cross-machine sync
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
    """``[push].mode`` for this project: "manual" or the default "land".

    An unknown value falls back to "land" -- the safe policy, since "manual"
    turns the push guard off.
    """
    mode = str(get(cwd, "push", "mode", project_name=project_name, default="land")).lower()
    return mode if mode in ("land", "manual") else "land"


def push_verify(cwd: str | None = None, project_name: str | None = None) -> bool:
    """``[push].verify``: whether the daemon's land push re-runs the repo's
    pre-push hook. Default False -- the daemon already ran ``.cabildo/gate.sh``
    immediately before, so re-verifying inside the push duplicates that work and,
    with a slow pre-push, times the push out (issue #14). Set true to keep the
    hook running on the daemon's push.
    """
    val = get(cwd, "push", "verify", project_name=project_name, default=False)
    return val is True or str(val).strip().lower() in ("1", "true", "yes", "on")


def push_timeout(cwd: str | None = None, project_name: str | None = None) -> float:
    """``[push].timeout_s``: seconds the land push may take before the daemon
    gives up. Default 300 -- git's plain 120 guillotines a slow network or a
    legitimately large push (issue #14).
    """
    val = get(cwd, "push", "timeout_s", project_name=project_name, default=300.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 300.0


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


def record_messages(cwd: str | None = None, project_name: str | None = None) -> bool:
    """``[record].messages`` for this project: default False.

    True opts this repo's conversation turns into cabildo's own DB (the daemon,
    started with --save-messages, ingests its transcripts). Off by default and
    per-project on purpose: a repo whose transcripts must never enter the shared
    bus simply never sets it, and nothing is recorded for it.
    """
    val = get(cwd, "record", "messages", project_name=project_name, default=False)
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
# [remote] -- cross-machine sync over a tunnel (wan.py)
# --------------------------------------------------------------------------- #
def remote_peers(cwd: str | None = None, project_name: str | None = None) -> list[str]:
    """``[remote].peers``: explicit ``"host:port"`` addresses to pull from over
    the tunnel. A bare string is accepted as a one-element list; anything else
    reads as no peers.
    """
    val = get(cwd, "remote", "peers", project_name=project_name, default=[])
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(p) for p in val]
    return []


def remote_token(cwd: str | None = None, project_name: str | None = None) -> str | None:
    """``[remote].token``: the shared bus token gating the pull handshake. ``None``
    when unset (an open bus, only safe behind a trusted tunnel).
    """
    val = get(cwd, "remote", "token", project_name=project_name, default=None)
    return str(val) if val else None


def remote_bind(cwd: str | None = None, project_name: str | None = None) -> str | None:
    """``[remote].bind``: the address to serve WAN pulls on (``"host"`` or
    ``"host:port"``), the tunnel or loopback interface. ``None`` means pull-only:
    do not serve.
    """
    val = get(cwd, "remote", "bind", project_name=project_name, default=None)
    return str(val) if val else None


def remote_interval(cwd: str | None = None, project_name: str | None = None) -> float:
    """``[remote].interval``: seconds between WAN sync rounds. Defaults to 30."""
    val = get(cwd, "remote", "interval", project_name=project_name, default=30.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 30.0
