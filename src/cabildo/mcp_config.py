"""Wire a folder for Claude Code: the `.mcp.json` half of `cabildo add`.

Claude Code reads `<repo>/.mcp.json` for **project-scoped** MCP servers, so
dropping cabildo in there means every session opened in that folder — yours,
a teammate's, a spawned agent's — joins the bus without anyone remembering to
register anything. That is the whole point: the folder carries its own wiring.

Two decisions worth stating:

  * The command is written as the bare `cabildo-mcp` when that name resolves on
    PATH. `.mcp.json` is a checked-in file; baking /home/you/.venv/bin/... into
    it hands your teammates a path that doesn't exist on their machines. Only
    when the name isn't resolvable do we fall back to this interpreter.
  * `CABILDO_PROJECT` is written into the entry's env. The local registry
    (~/.cabildo/cloud.json) already pins folder → name for this machine, but the
    env var travels with the repo, so a fresh clone gets the right project name
    without running `cabildo add` first.

Merging is conservative: other servers in the file are left untouched, and only
the `cabildo` key is written or removed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

SERVER_KEY = "cabildo"
FILENAME = ".mcp.json"


def mcp_command() -> list[str]:
    """How to launch the stdio MCP server, portably if possible."""
    if shutil.which("cabildo-mcp"):
        return ["cabildo-mcp"]
    sibling = Path(sys.executable).parent / "cabildo-mcp"
    if sibling.exists():
        return [str(sibling)]
    return [sys.executable, "-m", "cabildo.mcp_server"]


def server_entry(project: str | None = None) -> dict:
    """The `.mcp.json` value for the cabildo server."""
    cmd = mcp_command()
    entry: dict = {"command": cmd[0], "args": cmd[1:]}
    if project:
        entry["env"] = {"CABILDO_PROJECT": project}
    return entry


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


def apply(root: str | os.PathLike, project: str | None = None) -> tuple[Path, str]:
    """Ensure this folder's .mcp.json registers cabildo. Returns (path, what),
    where `what` is "added", "updated", or "unchanged"."""
    p = config_path(root)
    data = load(root)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{p}: 'mcpServers' is not an object")
    want = server_entry(project)
    had = servers.get(SERVER_KEY)
    if had == want:
        return p, "unchanged"
    servers[SERVER_KEY] = want
    _dump(p, data)
    return p, ("updated" if had is not None else "added")


def remove(root: str | os.PathLike) -> tuple[Path, str]:
    """Take cabildo back out. Deletes the file only if we'd be leaving behind an
    empty shell we created — a .mcp.json holding someone else's servers stays."""
    p = config_path(root)
    if not p.exists():
        return p, "absent"
    data = load(root)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return p, "absent"
    servers.pop(SERVER_KEY)
    if not servers and set(data) == {"mcpServers"}:
        p.unlink()
        return p, "deleted"
    _dump(p, data)
    return p, "removed"


def preview(root: str | os.PathLike, project: str | None = None) -> str:
    """What `apply` would write, as text — for `--print`."""
    data = load(root)
    servers = data.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        servers[SERVER_KEY] = server_entry(project)
    return json.dumps(data, indent=2) + "\n"
