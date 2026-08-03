"""Wire a folder for Claude Code: the `.mcp.json` half of `cabildo add`.

Claude Code reads `<repo>/.mcp.json` for **project-scoped** MCP servers, so
dropping cabildo in there means every session opened in that folder — yours,
a teammate's, a spawned agent's — joins the bus without anyone remembering to
register anything. That is the whole point: the folder carries its own wiring.

There are two servers, and they are deliberately different in scope:

  * **`cabildo`** — the bus. Other agents: who is here, messaging, leases.
  * **`issues`** — this repo's `docs/issues/` register. Only useful inside the
    project it is wired into, which is exactly why it belongs in `.mcp.json`
    rather than at user scope: a register is a property of a folder.

Neither is a daemon. Claude Code spawns each as a stdio subprocess, one per
session, so wiring them costs two lines of JSON and no running process.

Three decisions worth stating:

  * The command is written as the bare `cabildo-mcp` when that name resolves on
    PATH. `.mcp.json` is a checked-in file; baking /home/you/.venv/bin/... into
    it hands your teammates a path that doesn't exist on their machines. Only
    when the name isn't resolvable do we fall back to this interpreter.
  * `CABILDO_PROJECT` is written into the entry's env. The local registry
    (~/.cabildo/cloud.json) already pins folder → name for this machine, but the
    env var travels with the repo, so a fresh clone gets the right project name
    without running `cabildo add` first.
  * The issues server is wired only when the folder actually has one. A tool
    that appears in every session and answers "no docs/issues/ here" is a tool
    the model learns to ignore.

Merging is conservative: other servers in the file are left untouched, and only
our own keys are written or removed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

SERVER_KEY = "cabildo"
ISSUES_KEY = "issues"
OUR_KEYS = (SERVER_KEY, ISSUES_KEY)
FILENAME = ".mcp.json"


def _command(script: str, module: str) -> list[str]:
    """How to launch one of our stdio servers, portably if possible."""
    if shutil.which(script):
        return [script]
    sibling = Path(sys.executable).parent / script
    if sibling.exists():
        return [str(sibling)]
    return [sys.executable, "-m", module]


def mcp_command() -> list[str]:
    return _command("cabildo-mcp", "cabildo.mcp_server")


def issues_command() -> list[str]:
    return _command("cabildo-issues-mcp", "cabildo.issues_mcp")


def server_entry(project: str | None = None) -> dict:
    """The `.mcp.json` value for the cabildo bus server."""
    cmd = mcp_command()
    entry: dict = {"command": cmd[0], "args": cmd[1:]}
    if project:
        entry["env"] = {"CABILDO_PROJECT": project}
    return entry


def issues_entry() -> dict:
    """The `.mcp.json` value for the per-project issue register. No project env: this server reads
    the folder it is launched in, and has nothing to say about any other."""
    cmd = issues_command()
    return {"command": cmd[0], "args": cmd[1:]}


def has_issues(root: str | os.PathLike) -> bool:
    """Whether this folder keeps a `docs/issues/` register worth wiring a server for."""
    return Path(root).joinpath("docs", "issues").is_dir()


def config_path(root: str | os.PathLike) -> Path:
    return Path(root) / FILENAME


def load(root: str | os.PathLike) -> dict:
    """Existing config, or {}. A malformed file raises — silently overwriting
    something you hand-edited would be worse than the error."""
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


def _wanted(root: str | os.PathLike, project: str | None, issues: bool | None) -> dict:
    """The entries we want in this folder, keyed as they appear in .mcp.json."""
    want = {SERVER_KEY: server_entry(project)}
    if has_issues(root) if issues is None else issues:
        want[ISSUES_KEY] = issues_entry()
    return want


def apply(
    root: str | os.PathLike, project: str | None = None, issues: bool | None = None
) -> tuple[Path, str]:
    """Ensure this folder's .mcp.json registers our servers. Returns (path, what),
    where `what` is "added", "updated", or "unchanged".

    `issues` defaults to whether the folder has a `docs/issues/` — pass True to
    wire it anyway (you are about to create one), or False to leave it out.
    """
    p = config_path(root)
    data = load(root)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{p}: 'mcpServers' is not an object")
    want = _wanted(root, project, issues)
    had = {k: servers.get(k) for k in OUR_KEYS if k in servers}
    if all(servers.get(k) == v for k, v in want.items()):
        return p, "unchanged"
    servers.update(want)
    _dump(p, data)
    return p, ("updated" if had else "added")


def remove(root: str | os.PathLike) -> tuple[Path, str]:
    """Take our servers back out. Deletes the file only if we'd be leaving behind
    an empty shell we created — a .mcp.json holding someone else's servers stays."""
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


def preview(
    root: str | os.PathLike, project: str | None = None, issues: bool | None = None
) -> str:
    """What `apply` would write, as text — for `--print`."""
    data = load(root)
    servers = data.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        servers.update(_wanted(root, project, issues))
    return json.dumps(data, indent=2) + "\n"
