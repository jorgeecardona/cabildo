"""Wire a folder for Claude Code: the `.mcp.json` half of `cabildo add`.

Claude Code reads `<repo>/.mcp.json` for **project-scoped** MCP servers, so
dropping cabildo in there means every session opened in that folder — yours,
a teammate's, a spawned agent's — joins the bus without anyone remembering to
register anything. That is the whole point: the folder carries its own wiring.

One `cabildo` server is written. It carries both the bus tools (other agents:
who is here, messaging, leases) and this repo's issues/agenda tools, merged into
one server inside the package — so a session sees a single `mcp__cabildo__*`
surface. Earlier versions wrote a second `issues` entry; `apply` removes that
stale entry when it finds it, so re-running `cabildo add` migrates an old
`.mcp.json` in place.

It is not a daemon. Claude Code spawns it as a stdio subprocess, one per session,
so wiring costs a couple of lines of JSON and no running process.

Two decisions worth stating:

  * The command is written as the bare `cabildo-mcp` when a **login shell** can
    resolve that name. `.mcp.json` is a checked-in file; baking
    /home/you/.venv/bin/... into it hands your teammates a path that doesn't
    exist on their machines. But the test has to be the login shell and not this
    process: `cabildo add` may well run inside a venv that Claude Code's own
    environment never sees, and then the bare name looks fine here and fails in
    every session opened there. An absolute path costs a teammate; a name that
    resolves nowhere costs everyone, so when in doubt we write the path.
  * `CABILDO_PROJECT` is written into the entry's env. The local registry
    (~/.cabildo/cloud.json) already pins folder → name for this machine, but the
    env var travels with the repo, so a fresh clone gets the right project name
    without running `cabildo add` first.

Merging is conservative: other servers in the file are left untouched, and only
our own keys are written or removed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_KEY = "cabildo"
ISSUES_KEY = "issues"
OUR_KEYS = (SERVER_KEY, ISSUES_KEY)
FILENAME = ".mcp.json"


def _on_login_path(script: str) -> bool:
    """Can a *fresh login shell* resolve this name?

    `shutil.which` answers for the process writing the file, and that is the wrong process. Claude
    Code spawns MCP servers from its own environment, which on this machine does not inherit the
    venv that `cabildo add` happened to run inside. The bare name looked resolvable, got written,
    and every session in that folder failed with "Executable not found in $PATH" — a failure that
    surfaces nowhere near the command that caused it.

    So ask the shell the consumer looks most like. A false negative just means we write an absolute
    path, which always works; a false positive is what breaks a folder.
    """
    try:
        return (
            subprocess.run(
                ["bash", "-lc", f"command -v {shlex.quote(script)}"],
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return bool(shutil.which(script))  # no bash to ask — fall back to the weaker test


def _command(script: str, module: str) -> list[str]:
    """How to launch one of our stdio servers: portably when that genuinely works, absolutely when
    it does not. A path that only resolves on this machine is a cost to teammates; a name that
    resolves nowhere is a cost to everyone, including you.
    """
    if _on_login_path(script):
        return [script]
    sibling = Path(sys.executable).parent / script
    if sibling.exists():
        return [str(sibling)]
    if shutil.which(script):
        return [script]
    return [sys.executable, "-m", module]


def mcp_command() -> list[str]:
    return _command("cabildo-mcp", "cabildo.mcp_server")


def server_entry(project: str | None = None) -> dict:
    """The `.mcp.json` value for the cabildo server — bus + issues/agenda in one."""
    cmd = mcp_command()
    entry: dict = {"command": cmd[0], "args": cmd[1:]}
    if project:
        entry["env"] = {"CABILDO_PROJECT": project}
    return entry


def has_issues(root: str | os.PathLike) -> bool:
    """Whether this folder keeps a `docs/issues/` register worth wiring a server for."""
    return Path(root).joinpath("docs", "issues").is_dir()


def config_path(root: str | os.PathLike) -> Path:
    return Path(root) / FILENAME


def load(root: str | os.PathLike) -> dict:
    """Existing config, or {}. A malformed file raises — silently overwriting
    something you hand-edited would be worse than the error.
    """
    p = config_path(root)
    if not p.exists():
        return {}
    text = p.read_text().strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object")
    return data


def _dump(p: Path, data: dict) -> Path:
    p.write_text(json.dumps(data, indent=2) + "\n")
    return p


def _wanted(project: str | None) -> dict:
    """The entry we want in this folder: one `cabildo` server carrying bus and
    issues/agenda tools together.
    """
    return {SERVER_KEY: server_entry(project)}


def apply(
    root: str | os.PathLike, project: str | None = None, issues: bool | None = None
) -> tuple[Path, str]:
    """Ensure this folder's .mcp.json registers the cabildo server. Returns (path,
    what), where `what` is "added", "updated", or "unchanged".

    `issues` is accepted for backward compatibility and ignored: the one server now
    carries the register's tools, so there is nothing separate to wire. A stale
    `issues` entry from an earlier version is removed here, so re-running migrates
    an old file in place.
    """
    p = config_path(root)
    data = load(root)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{p}: 'mcpServers' is not an object")
    want = _wanted(project)
    stale_issues = ISSUES_KEY in servers  # a separate entry an earlier version wrote
    if not stale_issues and all(servers.get(k) == v for k, v in want.items()):
        return p, "unchanged"
    had = {k: servers.get(k) for k in OUR_KEYS if k in servers}
    servers.update(want)
    servers.pop(ISSUES_KEY, None)
    _dump(p, data)
    return p, ("updated" if had else "added")


def remove(root: str | os.PathLike) -> tuple[Path, str]:
    """Take our servers back out. Deletes the file only if we'd be leaving behind
    an empty shell we created — a .mcp.json holding someone else's servers stays.
    """
    p = config_path(root)
    if not p.exists():
        return p, "absent"
    data = load(root)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or not any(k in servers for k in OUR_KEYS):
        return p, "absent"
    for key in OUR_KEYS:
        servers.pop(key, None)
    if not servers and set(data) == {"mcpServers"}:
        p.unlink()
        return p, "deleted"
    _dump(p, data)
    return p, "removed"


def preview(root: str | os.PathLike, project: str | None = None, issues: bool | None = None) -> str:
    """What `apply` would write, as text — for `--print`. `issues` is ignored (kept
    for signature compatibility).
    """
    data = load(root)
    servers = data.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        servers.update(_wanted(project))
        servers.pop(ISSUES_KEY, None)
    return json.dumps(data, indent=2) + "\n"
