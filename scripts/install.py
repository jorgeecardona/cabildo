#!/usr/bin/env python3
"""One-shot installer for agent-chat.

  * creates the project venv and installs the package (via uv),
  * registers the MCP server at USER scope (available in every project),
  * appends the three hooks to ~/.claude/settings.json, preserving whatever is
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
MCP_BIN = VENV / "bin" / "agent-chat-mcp"
SETTINGS = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = REPO / "agent_chat" / "hooks"

# The hooks are stdlib-only, but we run them with the project venv's python so
# the command is self-contained and can't be broken by PATH changes.
HOOK_PY = str(VENV_PY)

HOOKS = {
    "SessionStart": ("session_start.py", 10),
    "UserPromptSubmit": ("prompt_submit.py", 10),
    "SessionEnd": ("session_end.py", 10),
}


def sh(cmd: list[str]) -> int:
    print("  $", " ".join(cmd))
    return subprocess.call(cmd)


def ensure_venv() -> None:
    if not VENV_PY.exists():
        print("• creating venv")
        sh(["uv", "venv", "--python", "3.13", str(VENV)])
    print("• installing package into venv")
    sh(["uv", "pip", "install", "--python", str(VENV_PY), "-e", str(REPO)])


def register_mcp() -> None:
    print("• registering MCP server (user scope)")
    # Remove any prior entry first so this is idempotent, then add.
    subprocess.call(["claude", "mcp", "remove", "--scope", "user", "agent-chat"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = sh(["claude", "mcp", "add", "--scope", "user", "agent-chat", "--",
             str(MCP_BIN)])
    if rc != 0:
        print("  ! `claude mcp add` failed — register manually with the command above")


def hook_command(script: str) -> str:
    return f"{HOOK_PY} {HOOKS_DIR / script}"


def patch_settings() -> None:
    print(f"• wiring hooks into {SETTINGS}")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = data.setdefault("hooks", {})

    for event, (script, timeout) in HOOKS.items():
        cmd = hook_command(script)
        groups = hooks.setdefault(event, [])
        already = any(
            "agent_chat/hooks/" + script in h.get("command", "")
            for g in groups
            for h in g.get("hooks", [])
        )
        if already:
            print(f"  = {event}: already present, skipping")
            continue
        groups.append({"hooks": [{"type": "command", "command": cmd,
                                  "timeout": timeout}]})
        print(f"  + {event}: {cmd}")

    if SETTINGS.exists():
        shutil.copy(SETTINGS, SETTINGS.with_suffix(".json.bak"))
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    print(f"agent-chat installer  (repo: {REPO})\n")
    ensure_venv()
    register_mcp()
    patch_settings()
    print(
        "\nDone. Restart any running Claude Code sessions so they pick up the new "
        "MCP server and hooks. New sessions will auto-join the bus."
    )


if __name__ == "__main__":
    main()
