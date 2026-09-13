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

import json
import shlex
import shutil
import subprocess
from pathlib import Path

from .mcp_config import _command, mcp_command

SERVER_NAME = "cabildo"

# Codex lifecycle events cabildo wires (see cabildo.codex_hooks). PreToolUse is
# the claim guard; the rest are registration + unread/urgent delivery.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")
_TOOL_EVENTS = {"PreToolUse", "PostToolUse"}  # these take a tool-name `matcher`
HOOK_TIMEOUT = 10


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


# --------------------------------------------------------------------------- #
# Hooks: delivery + the claim guard (cabildo.codex_hooks). Unlike the MCP entry,
# Codex has no CLI to manage hooks — but hooks.json is plain JSON, so we merge it
# ourselves the same conservative way mcp_config edits .mcp.json: only our own
# command entries are added or removed, everything else is left untouched.
# --------------------------------------------------------------------------- #
def hook_command() -> list[str]:
    """Launch argv for the Codex hook dispatcher, resolved portably like the MCP
    command (bare name when a login shell finds it, else an absolute path).
    """
    return _command("cabildo-codex-hook", "cabildo.codex_hooks")


def _hook_command_str(event: str) -> str:
    """The single shell string Codex stores as a hook `command`: our dispatcher
    plus the event name it should handle.
    """
    return shlex.join([*hook_command(), event])


def hooks_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def _is_ours(entry: object) -> bool:
    """True if a hooks.json event-entry is one cabildo wrote (any handler in it
    invokes `cabildo-codex-hook`).
    """
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and "cabildo-codex-hook" in (h.get("command") or "") for h in hooks
    )


def _our_entry(event: str) -> dict:
    entry: dict = {
        "hooks": [{"type": "command", "command": _hook_command_str(event), "timeout": HOOK_TIMEOUT}]
    }
    if event in _TOOL_EVENTS:
        # Match every tool so the guard sees Bash, edits, and MCP tool calls.
        return {"matcher": ".*", **entry}
    return entry


def hooks_config(existing: dict | None = None) -> dict:
    """`existing` (a parsed hooks.json) with cabildo's entries ensured present.

    Our entry for each event is replaced in place (or appended if absent);
    unrelated events and other tools' entries are preserved, and position is
    kept stable so re-running is a no-op.
    """
    data = json.loads(json.dumps(existing)) if existing else {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json: 'hooks' is not an object")
    for event in HOOK_EVENTS:
        arr = hooks.setdefault(event, [])
        if not isinstance(arr, list):
            raise ValueError(f"hooks.json: hooks.{event} is not an array")
        ours = [i for i, e in enumerate(arr) if _is_ours(e)]
        fresh = _our_entry(event)
        if ours:
            arr[ours[0]] = fresh
            for i in reversed(ours[1:]):  # collapse any duplicates
                del arr[i]
        else:
            arr.append(fresh)
    return data


def load_hooks() -> dict:
    p = hooks_path()
    if not p.exists():
        return {}
    text = p.read_text().strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object")
    return data


def apply_hooks() -> tuple[Path, str]:
    """Ensure ~/.codex/hooks.json wires cabildo's hooks. Returns (path, what)."""
    p = hooks_path()
    existing = load_hooks()
    want = hooks_config(existing)
    if existing == want:
        return p, "unchanged"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(want, indent=2) + "\n")
    return p, ("updated" if existing else "added")


def remove_hooks() -> tuple[Path, str]:
    """Take cabildo's hook entries back out, leaving anyone else's intact."""
    p = hooks_path()
    if not p.exists():
        return p, "absent"
    data = load_hooks()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return p, "absent"
    changed = False
    for event, arr in list(hooks.items()):
        if not isinstance(arr, list):
            continue
        kept = [e for e in arr if not _is_ours(e)]
        if len(kept) != len(arr):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not changed:
        return p, "absent"
    if not hooks and set(data) == {"hooks"}:
        p.unlink()
        return p, "deleted"
    p.write_text(json.dumps(data, indent=2) + "\n")
    return p, "removed"


def hooks_preview() -> str:
    """What apply_hooks would write, as text — for `--print`."""
    return json.dumps(hooks_config(load_hooks()), indent=2) + "\n"
