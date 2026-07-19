#!/usr/bin/env python3
"""One-shot installer for cabildo.

  * creates the project venv and installs the package (via uv),
  * registers the MCP server at USER scope (available in every project),
  * appends the hooks to ~/.claude/settings.json, preserving whatever is
    already there.

Idempotent: re-running won't duplicate hooks or the MCP entry. Run with
`python scripts/install.py`; pass --print to only show what it would do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV = REPO / ".venv"
VENV_PY = VENV / "bin" / "python"
MCP_BIN = VENV / "bin" / "cabildo-mcp"
HOOK_BIN = VENV / "bin" / "cabildo-hook"
SETTINGS = Path.home() / ".claude" / "settings.json"

# event -> (cabildo-hook arg, timeout_s, tool matcher or None). The
# per-tool-call hooks get a tight timeout: they run on every single tool call,
# so a hung DB must cost 5s once, not block the whole session repeatedly.
HOOKS = {
    "SessionStart": ("session_start", 10, None),
    "UserPromptSubmit": ("prompt_submit", 10, None),
    "SessionEnd": ("session_end", 10, None),
    "Stop": ("stop", 10, None),
    "PostToolUse": ("post_tool_use", 5, "*"),
    "PreToolUse": ("pre_tool_use", 5, "Bash|Edit|Write|NotebookEdit"),
}


def sh(cmd: list[str]) -> int:
    print("  $", " ".join(cmd))
    return subprocess.call(cmd)


def ensure_venv() -> None:
    if not VENV_PY.exists():
        print("• creating venv")
        sh(["uv", "venv", "--python", "3.13", str(VENV)])
    # Pre-rename installs left an `agent-chat` dist with stale entry points.
    subprocess.call(["uv", "pip", "uninstall", "--python", str(VENV_PY), "agent-chat"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("• installing package into venv")
    sh(["uv", "pip", "install", "--python", str(VENV_PY), "-e", str(REPO)])


def migrate_legacy_home() -> None:
    """Pre-rename installs kept the bus in ~/.agent-chat. Move it once."""
    old = Path.home() / ".agent-chat"
    new = Path.home() / ".cabildo"
    if old.exists() and not new.exists():
        print(f"• migrating data dir {old} -> {new}")
        shutil.move(str(old), str(new))


def register_mcp() -> None:
    print("• registering MCP server (user scope)")
    # Remove any prior entry first so this is idempotent, then add. Also drop
    # the pre-rename "agent-chat" registration.
    subprocess.call(["claude", "mcp", "remove", "--scope", "user", "agent-chat"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["claude", "mcp", "remove", "--scope", "user", "cabildo"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = sh(["claude", "mcp", "add", "--scope", "user", "cabildo", "--",
             str(MCP_BIN)])
    if rc != 0:
        print("  ! `claude mcp add` failed — register manually with the command above")


def hook_command(event_arg: str) -> str:
    return f"{HOOK_BIN} {event_arg}"


def patch_settings() -> None:
    print(f"• wiring hooks into {SETTINGS}")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = data.setdefault("hooks", {})

    # Drop stale entries from older installs: repo-file-path commands
    # (agent_chat/ or cabildo/ hook scripts) break whenever the repo
    # moves or a file is renamed — exactly why we dispatch via HOOK_BIN now.
    for event, groups in list(hooks.items()):
        kept = [g for g in groups
                if not any("_chat/hooks/" in h.get("command", "")
                           or "cabildo/hooks/" in h.get("command", "")
                           for h in g.get("hooks", []))]
        if len(kept) != len(groups):
            print(f"  - {event}: removed stale file-path entry")
        hooks[event] = kept

    for event, (event_arg, timeout, matcher) in HOOKS.items():
        cmd = hook_command(event_arg)
        groups = hooks.setdefault(event, [])
        already = any(
            h.get("command", "") == cmd
            for g in groups
            for h in g.get("hooks", [])
        )
        if already:
            print(f"  = {event}: already present, skipping")
            continue
        group = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
        print(f"  + {event}: {cmd}")

    if SETTINGS.exists():
        shutil.copy(SETTINGS, SETTINGS.with_suffix(".json.bak"))
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    print(f"cabildo installer  (repo: {REPO})\n")
    ensure_venv()
    migrate_legacy_home()
    register_mcp()
    patch_settings()
    print(
        "\nDone. Restart any running Claude Code sessions so they pick up the new "
        "MCP server and hooks. New sessions will auto-join the bus."
    )


if __name__ == "__main__":
    main()
