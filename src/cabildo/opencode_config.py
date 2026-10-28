"""Wire opencode onto the same cabildo bus Claude Code and Codex use.

opencode reads MCP servers from its JSON config (`$XDG_CONFIG_HOME/opencode/
opencode.json`, default `~/.config/opencode/opencode.json`) under an `"mcp"`
object, each a `{"type": "local", "command": [...], "enabled": true,
"environment": {...}}` entry. It has no `mcp add`-style CLI we can lean on the way
Codex does, so we merge the JSON ourselves the conservative way `mcp_config` edits
`.mcp.json`: only our own `cabildo` entry is written or removed, every other server
and unrelated key is left untouched.

Two things, both consequences of client-independent identity (see `mcp_server._me`):

  * `CABILDO_CLIENT=opencode` is injected so the MCP server tags this session's bus
    identity as opencode, not Claude — that is what keeps an opencode session
    distinct in `list_agents`/`whoami`/the viewer. Without it every opencode session
    masquerades as `claude` and the Claude PID rendezvous misfires.
  * `CABILDO_SESSION_ID` is not written: opencode's static `env` is identical for
    every session, so it identifies via the client-scoped owner-pid path instead.

We also drop cabildo's generic `/agenda` and `/sanity` commands into opencode's
command directory, so the read works out of the box. `CABILDO_HOME` is pinned so all
clients provably share one bus DB.

Hooks (the claim guard) are not wired here: opencode has no cabildo hook adapter yet,
so an opencode session gets messaging, presence, and advisory claims, but not
hook-enforced file leases. That is the same graceful degradation a bus-less client
already accepts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .mcp_config import mcp_command

SERVER_NAME = "cabildo"
COMMANDS = ("agenda.md", "sanity.md")


def server_env(project: str | None = None, home: str | None = None) -> dict[str, str]:
    """Environment cabildo needs on the opencode-spawned MCP server. Ordered so the
    written JSON is stable (idempotent) across runs.
    """
    env: dict[str, str] = {"CABILDO_CLIENT": "opencode"}
    if home:
        env["CABILDO_HOME"] = home
    if project:
        env["CABILDO_PROJECT"] = project
    return env


def mcp_entry(project: str | None = None, home: str | None = None) -> dict:
    """The opencode `mcp.cabildo` value: the same `cabildo-mcp` launch the other
    clients use, tagged as opencode.
    """
    return {
        "type": "local",
        "command": mcp_command(),
        "enabled": True,
        "environment": server_env(project, home),
    }


def _config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".config") / "opencode"


def config_path() -> Path:
    return _config_root() / "opencode.json"


def command_dir() -> Path:
    return _config_root() / "command"


def _commands_source() -> Path:
    return Path(__file__).parent / "commands"


def load(path: Path) -> dict:
    """Opencode's config, or {}. A malformed file raises rather than being silently
    overwritten.
    """
    if not path.exists():
        return {}
    text = path.read_text().strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _servers(data: dict) -> dict:
    servers = data.setdefault("mcp", {})
    if not isinstance(servers, dict):
        raise ValueError("opencode.json: 'mcp' is not an object")
    return servers


def install_commands() -> list[Path]:
    """Copy cabildo's generic /agenda and /sanity into opencode's command dir. Only
    our own files are written; anything else there is left alone.
    """
    dest = command_dir()
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name in COMMANDS:
        target = dest / name
        target.write_text((_commands_source() / name).read_text())
        written.append(target)
    return written


def remove_commands() -> list[Path]:
    dest = command_dir()
    removed = []
    for name in COMMANDS:
        target = dest / name
        if target.exists():
            target.unlink()
            removed.append(target)
    return removed


def apply(project: str | None = None, home: str | None = None) -> tuple[Path, str]:
    """Ensure opencode's config registers the cabildo MCP server (tagged opencode) and
    that its commands are installed. Returns (config_path, what).
    """
    p = config_path()
    data = load(p)
    servers = _servers(data)
    want = mcp_entry(project, home)
    had = SERVER_NAME in servers
    unchanged = servers.get(SERVER_NAME) == want
    if not unchanged:
        servers[SERVER_NAME] = want
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n")
    install_commands()  # always refresh the bundled commands
    return p, ("unchanged" if unchanged else ("updated" if had else "added"))


def remove() -> tuple[Path, str]:
    """Take cabildo back out of opencode: drop our MCP entry and our commands, leaving
    everything else.
    """
    remove_commands()
    p = config_path()
    if not p.exists():
        return p, "absent"
    data = load(p)
    servers = data.get("mcp")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return p, "absent"
    del servers[SERVER_NAME]
    p.write_text(json.dumps(data, indent=2) + "\n")
    return p, "removed"


def preview(project: str | None = None, home: str | None = None) -> str:
    """What apply would write into opencode.json — for `--print`."""
    data = load(config_path())
    if isinstance(data.get("mcp", {}), dict):
        _servers(data)[SERVER_NAME] = mcp_entry(project, home)
    return json.dumps(data, indent=2) + "\n"
