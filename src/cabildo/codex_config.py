"""Wire Codex onto the same cabildo bus Claude Code uses.

Codex reads MCP servers from `~/.codex/config.toml` (user scope) or
`<repo>/.codex/config.toml` (project scope) as `[mcp_servers.<id>]` tables, and
ships a CLI to manage them: `codex mcp add <name> --env K=V -- <command>`. We
delegate to that CLI rather than editing the TOML ourselves — Codex owns its
config file (comments, formatting, unrelated servers), and reimplementing a
TOML round-trip here would add a dependency and risk clobbering hand edits.
When `codex` is not on PATH we print the exact `[mcp_servers.cabildo]` block for
the user to paste, which doubles as the `--print` preview.

Two decisions worth stating, both consequences of #6 (client-independent
identity):

  * `CABILDO_CLIENT=codex` is injected so the MCP server tags this session's
    bus identity as Codex, not Claude — that is what keeps a Codex session
    distinct in `list_agents`/`whoami`/the viewer.
  * `CABILDO_SESSION_ID` is deliberately NOT written. Codex does not template a
    fresh per-session id into `config.toml` (its static `env` values would be
    identical for every session, collapsing them onto one bus identity). A
    Codex session therefore identifies via the client-scoped owner-pid path in
    `mcp_server._me`. A stable id that survives reconnect would come from a
    Codex SessionStart hook (issue #10), which is where Codex exposes a real
    `session_id`.

`CABILDO_HOME` is pinned so both clients provably share one bus DB; when unset
it equals the default `~/.cabildo`, so pinning changes nothing but ambiguity.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .mcp_config import mcp_command

SERVER_NAME = "cabildo"


def server_env(project: str | None = None, home: str | None = None) -> dict[str, str]:
    """Environment cabildo needs on the Codex-spawned MCP server. Ordered so the
    generated command/TOML is stable (idempotent) across runs.
    """
    env: dict[str, str] = {"CABILDO_CLIENT": "codex"}
    if home:
        env["CABILDO_HOME"] = home
    if project:
        env["CABILDO_PROJECT"] = project
    return env


def add_argv(
    project: str | None = None, home: str | None = None, name: str = SERVER_NAME
) -> list[str]:
    """The `codex mcp add` command that registers cabildo as a stdio server.

    `--env K=V` is repeatable and stdio-only; everything after `--` is the
    server launch command, reused verbatim from the Claude wiring so both
    clients start the identical `cabildo-mcp`.
    """
    argv = ["codex", "mcp", "add", name]
    for key, val in server_env(project, home).items():
        argv += ["--env", f"{key}={val}"]
    argv += ["--", *mcp_command()]
    return argv


def remove_argv(name: str = SERVER_NAME) -> list[str]:
    """`codex mcp remove <name>`. The subcommand name is not confirmed against
    the official docs, so callers should surface its output rather than trust it
    silently.
    """
    return ["codex", "mcp", "remove", name]


def _toml_str(s: str) -> str:
    """A TOML basic string: escape backslash and double-quote."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_block(project: str | None = None, home: str | None = None, name: str = SERVER_NAME) -> str:
    """The `[mcp_servers.<name>]` block to paste into `~/.codex/config.toml`.

    This is the manual fallback (no `codex` on PATH) and the `--print` preview.
    It writes only our own table, never touching the rest of the file.
    """
    cmd = mcp_command()
    args = ", ".join(_toml_str(a) for a in cmd[1:])
    lines = [
        f"[mcp_servers.{name}]",
        f"command = {_toml_str(cmd[0])}",
        f"args = [{args}]",
        "",
        f"[mcp_servers.{name}.env]",
    ]
    lines += [f"{key} = {_toml_str(val)}" for key, val in server_env(project, home).items()]
    return "\n".join(lines) + "\n"


def codex_on_path() -> bool:
    return shutil.which("codex") is not None


def run_add(
    project: str | None = None, home: str | None = None, name: str = SERVER_NAME
) -> subprocess.CompletedProcess:
    """Register via Codex's own CLI. `codex mcp add` overwrites an existing
    entry of the same name, so re-running is idempotent (Codex owns the merge).
    Raises if `codex` is not on PATH — callers check `codex_on_path()` first.
    """
    return subprocess.run(add_argv(project, home, name), capture_output=True, text=True)


def config_paths() -> tuple[Path, Path]:
    """(user, project) config.toml locations, for messaging. We do not read or
    write these — Codex does — but users ask where its config lives.
    """
    return Path.home() / ".codex" / "config.toml", Path(".codex") / "config.toml"
