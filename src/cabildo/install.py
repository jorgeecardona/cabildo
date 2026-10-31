"""Wire cabildo into Claude Code: the MCP server, the hooks and the push mod.

Used by ``cabildo install`` (bin directory = the venv running the command) and by
``scripts/install.py`` (bin directory = the repo ``.venv``). Every function takes the
paths it writes, so both callers share one implementation. Re-running changes nothing.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from cabildo import doctor, mods

# event -> (cabildo-hook arg, timeout_s, matcher or None). The per-tool-call hooks get a
# tight timeout: they run on every tool call, so a hung DB must cost 5s once.
HOOKS: dict[str, tuple[str, int, str | None]] = {
    "SessionStart": ("session_start", 10, None),
    "UserPromptSubmit": ("prompt_submit", 10, None),
    "SessionEnd": ("session_end", 10, None),
    "Stop": ("stop", 10, None),
    "StopFailure": ("stop_failure", 10, "rate_limit"),
    "SubagentStop": ("subagent_stop", 10, None),
    "PostToolUse": ("post_tool_use", 5, "*"),
    "PreToolUse": ("pre_tool_use", 5, "Bash|Edit|Write|NotebookEdit"),
}


class InstallError(RuntimeError):
    """A step that cannot proceed: a missing executable or a failed subprocess."""


def require(tool: str) -> str:
    """Absolute path to ``tool``; raises ``InstallError`` when it is not on PATH."""
    found = shutil.which(tool)
    if not found:
        msg = f"required executable not found on PATH: {tool!r}"
        raise InstallError(msg)
    return found


def sh(cmd: list[str], *, check: bool = True) -> int:
    """Run ``cmd``, echoing it. Raises ``InstallError`` on failure unless ``check`` is False."""
    print("  $", " ".join(shlex.quote(c) for c in cmd))
    rc = subprocess.call(cmd)
    if check and rc != 0:
        msg = f"command failed (exit {rc}): {' '.join(shlex.quote(c) for c in cmd)}"
        raise InstallError(msg)
    return rc


def hook_command(hook_bin: Path, event_arg: str) -> str:
    """The shell string a Claude hook stores as its ``command``.

    The path is shell-quoted so a venv under a directory with spaces still works. For an
    ordinary path the quoting is a no-op, so re-running stays a no-op.
    """
    return f"{shlex.quote(str(hook_bin))} {event_arg}"


def register_mcp(mcp_bin: Path) -> None:
    """Register the MCP server at user scope. Removes any older entry first."""
    print("• registering MCP server (user scope)")
    require("claude")
    for name in ("agent-chat", "cabildo"):  # the pre-rename name, then the current one
        subprocess.call(
            ["claude", "mcp", "remove", "--scope", "user", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    sh(["claude", "mcp", "add", "--scope", "user", "cabildo", "--", str(mcp_bin)])


def patch_settings(settings: Path, hook_bin: Path, cabildo_bin: Path) -> None:
    """Add cabildo's hooks and ``env.CABILDO_BIN`` to ``settings``, keeping other entries."""
    print(f"• wiring hooks into {settings}")
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(settings.read_text()) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})

    # Older installs stored repo file paths, which break when the repo moves.
    for event, groups in list(hooks.items()):
        kept = [
            g
            for g in groups
            if not any(
                "_chat/hooks/" in h.get("command", "") or "cabildo/hooks/" in h.get("command", "")
                for h in g.get("hooks", [])
            )
        ]
        if len(kept) != len(groups):
            print(f"  - {event}: removed stale file-path entry")
        hooks[event] = kept

    for event, (event_arg, timeout, matcher) in HOOKS.items():
        cmd = hook_command(hook_bin, event_arg)
        groups = hooks.setdefault(event, [])
        if any(h.get("command", "") == cmd for g in groups for h in g.get("hooks", [])):
            print(f"  = {event}: already present, skipping")
            continue
        group: dict[str, object] = {
            "hooks": [{"type": "command", "command": cmd, "timeout": timeout}]
        }
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
        print(f"  + {event}: {cmd}")

    # The push mod shells the ``cabildo`` CLI; pin the binary so it is found off PATH.
    env = data.setdefault("env", {})
    if env.get("CABILDO_BIN") != str(cabildo_bin):
        env["CABILDO_BIN"] = str(cabildo_bin)
        print(f"  + env.CABILDO_BIN = {cabildo_bin}")

    if settings.exists():
        shutil.copy(settings, settings.with_suffix(".json.bak"))
    settings.write_text(json.dumps(data, indent=2) + "\n")


def register_plugin() -> None:
    """Install and enable the push mod at user scope. Not fatal: the bus works without it."""
    print("• installing the cabildo-push mod (user scope)")
    res = mods.install(lambda argv: sh(argv, check=False), scope="user")
    if not res.ok:
        print(f"  ! {res.reason} — skipping the mod (the bus still works)")
        return
    for line in mods.step_warnings(res):
        print(f"  ! {line}")


def preview_claude(
    settings: Path, mcp_bin: Path, hook_bin: Path, cabildo_bin: Path, say: Callable[[str], None]
) -> None:
    """Describe the Claude steps through ``say`` and change nothing."""
    say("\n• Claude: register the MCP server (user scope):")
    say(f"    $ claude mcp add --scope user cabildo -- {mcp_bin}")
    say(f"• Claude: wire hooks into {settings} (existing entries preserved):")
    for event, (event_arg, *_rest) in HOOKS.items():
        say(f"    {event}: {hook_command(hook_bin, event_arg)}")
    say(f"    env.CABILDO_BIN = {cabildo_bin}")
    if mods.mods_dir() is not None:
        say("• Claude: install the push-delivery mod (user scope):")
        for argv in mods.plan(scope="user"):
            say(f"    $ {' '.join(argv)}")


def run(clients: tuple[str, ...], *, print_only: bool = False) -> int:
    """``cabildo install``: wire ``clients`` using the venv this process runs from."""
    bin_dir = Path(sys.executable).parent
    mcp_bin, hook_bin, cabildo_bin = (
        bin_dir / n for n in ("cabildo-mcp", "cabildo-hook", "cabildo")
    )
    settings = doctor.settings_path()
    if print_only:
        print("cabildo install — DRY RUN (--print): nothing will be changed")
        if "claude" in clients:
            preview_claude(settings, mcp_bin, hook_bin, cabildo_bin, print)
        for client in ("codex", "opencode"):
            if client in clients:
                print(f"\n• {client}: run `{cabildo_bin} {client} --print`")
        return 0
    try:
        if "claude" in clients:
            register_mcp(mcp_bin)
            patch_settings(settings, hook_bin, cabildo_bin)
            register_plugin()
        for client in ("codex", "opencode"):
            if client in clients:
                print(f"• wiring {client} (via `cabildo {client}`)")
                sh([str(cabildo_bin), client])
    except InstallError as e:
        print(f"\n! {e}", file=sys.stderr)
        return 1
    print("\nDone. Restart running Claude Code sessions to load the MCP server, hooks and mod.")
    return 0
