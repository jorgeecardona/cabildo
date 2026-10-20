#!/usr/bin/env python3
"""One-shot installer for cabildo.

  * creates the project venv and installs the package (via uv),
  * for the **Claude** client: registers the MCP server at USER scope
    (available in every project) and appends the hooks to
    ~/.claude/settings.json, preserving whatever is already there,
  * for the **Codex** client: delegates to `cabildo codex`, which registers the
    MCP server with Codex (`codex mcp add`, or prints the config block if codex
    is not on PATH) and merges cabildo's hooks into ~/.codex/hooks.json.
  * for the **opencode** client: delegates to `cabildo opencode`, which registers
    the MCP server in opencode's JSON config (tagged opencode) and installs the
    /agenda + /sanity commands. opencode has no cabildo hook adapter yet, so it
    gets messaging, presence and advisory claims, not hook-enforced leases.

Choose clients with `--client {claude,codex,opencode,both,all}` (default: claude,
which preserves the historical behavior; `both` = claude+codex, `all` = every
client). Idempotent: re-running won't duplicate hooks or the MCP entry. Pass
`--print` to show what it would do and change nothing — no venv, no package
install, no data migration, no registration, no hook writes.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV = REPO / ".venv"
VENV_PY = VENV / "bin" / "python"
MCP_BIN = VENV / "bin" / "cabildo-mcp"
HOOK_BIN = VENV / "bin" / "cabildo-hook"
CABILDO_BIN = VENV / "bin" / "cabildo"
SETTINGS = Path.home() / ".claude" / "settings.json"

# The bundled Claude Code mod (push delivery, GitLab #30) is installed at user
# scope so every session on the machine loads it; the mod shells the `cabildo`
# CLI, found via env.CABILDO_BIN below. The marketplace root, the plugin id, and
# the `claude plugin` sequence all live in `cabildo.mods` (single-sourced with
# the `cabildo plugin` CLI verb); this script only drives it.

CLIENTS = ("claude", "codex", "opencode", "both", "all")

# event -> (cabildo-hook arg, timeout_s, tool matcher or None). The
# per-tool-call hooks get a tight timeout: they run on every single tool call,
# so a hung DB must cost 5s once, not block the whole session repeatedly.
HOOKS = {
    "SessionStart": ("session_start", 10, None),
    "UserPromptSubmit": ("prompt_submit", 10, None),
    "SessionEnd": ("session_end", 10, None),
    "Stop": ("stop", 10, None),
    "SubagentStop": ("subagent_stop", 10, None),
    "PostToolUse": ("post_tool_use", 5, "*"),
    "PreToolUse": ("pre_tool_use", 5, "Bash|Edit|Write|NotebookEdit"),
}


class InstallError(RuntimeError):
    """A step that cannot proceed: a missing executable or a failed subprocess.

    Raised so the failure surfaces with a clear message and a non-zero exit,
    rather than a half-done install limping on.
    """


def require(tool: str) -> str:
    """Absolute path to `tool`, or a clear InstallError if it is not on PATH."""
    found = shutil.which(tool)
    if not found:
        raise InstallError(f"required executable not found on PATH: {tool!r}")
    return found


def sh(cmd: list[str], *, check: bool = True) -> int:
    """Run `cmd`, echoing it shell-safely. Raises InstallError on failure unless
    check=False (used for best-effort cleanup that is allowed to fail).
    """
    print("  $", " ".join(shlex.quote(c) for c in cmd))
    rc = subprocess.call(cmd)
    if check and rc != 0:
        raise InstallError(f"command failed (exit {rc}): {' '.join(shlex.quote(c) for c in cmd)}")
    return rc


def hook_command(event_arg: str) -> str:
    """The single shell string a Claude hook stores as its `command`: the
    dispatcher binary plus the event arg. The path is shell-quoted so a venv
    living under a directory with spaces still produces a valid command (for an
    ordinary path shlex.quote is a no-op, so this stays byte-identical to older
    installs and re-running remains a no-op).
    """
    return f"{shlex.quote(str(HOOK_BIN))} {event_arg}"


def ensure_venv() -> None:
    require("uv")
    if not VENV_PY.exists():
        print("• creating venv")
        sh(["uv", "venv", "--python", "3.13", str(VENV)])
    # Pre-rename installs left an `agent-chat` dist with stale entry points.
    subprocess.call(
        ["uv", "pip", "uninstall", "--python", str(VENV_PY), "agent-chat"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
    require("claude")
    # Remove any prior entry first so this is idempotent, then add. Also drop
    # the pre-rename "agent-chat" registration. These removes are best-effort:
    # they exit non-zero when nothing is registered, which is fine.
    subprocess.call(
        ["claude", "mcp", "remove", "--scope", "user", "agent-chat"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.call(
        ["claude", "mcp", "remove", "--scope", "user", "cabildo"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sh(["claude", "mcp", "add", "--scope", "user", "cabildo", "--", str(MCP_BIN)])


def patch_settings() -> None:
    print(f"• wiring hooks into {SETTINGS}")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = data.setdefault("hooks", {})

    # Drop stale entries from older installs: repo-file-path commands
    # (agent_chat/ or cabildo/ hook scripts) break whenever the repo
    # moves or a file is renamed — exactly why we dispatch via HOOK_BIN now.
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
        cmd = hook_command(event_arg)
        groups = hooks.setdefault(event, [])
        already = any(h.get("command", "") == cmd for g in groups for h in g.get("hooks", []))
        if already:
            print(f"  = {event}: already present, skipping")
            continue
        group = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
        print(f"  + {event}: {cmd}")

    # The push-delivery mod shells the `cabildo` CLI from $.process.run, which
    # inherits the session env. Pin the venv's binary so the mod finds it even
    # when the venv is not on PATH.
    env = data.setdefault("env", {})
    if env.get("CABILDO_BIN") != str(CABILDO_BIN):
        env["CABILDO_BIN"] = str(CABILDO_BIN)
        print(f"  + env.CABILDO_BIN = {CABILDO_BIN}")

    if SETTINGS.exists():
        shutil.copy(SETTINGS, SETTINGS.with_suffix(".json.bak"))
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def register_plugin() -> None:
    """Install and enable the bundled push-delivery mod at user scope, reusing
    the package's own `cabildo.mods.install` so the `claude plugin` sequence
    lives in one place (the `cabildo plugin` CLI verb runs the same code).
    Non-fatal: an older `claude` without `plugin` subcommands, or a failed
    validate, is reported and skipped — messaging, presence and claims work
    without the mod; it only adds the idle-wake doorbell.
    """
    print("• installing the cabildo-push mod (user scope)")
    mods = _mods()
    # The module runs the same commands `sh` would, echoing each; check=False
    # because its exit codes ride along in the Result, not as a hard failure.
    # mods.install is the single check: no bundled mod, no `claude`, or a failed
    # validate each come back as ok=False with a reason, honouring CABILDO_MODS_DIR.
    res = mods.install(lambda argv: sh(argv, check=False), scope="user")
    if not res.ok:
        print(f"  ! {res.reason} — skipping the mod (the bus still works)")
    else:
        failed = [s for s in res.steps if s.rc != 0]
        for s in failed:
            print(f"  ! exit {s.rc}: {' '.join(s.argv)}")


def install_codex() -> None:
    """Wire the Codex client by delegating to the installed `cabildo codex`,
    which owns the (tested) Codex registration + hooks-merge logic. This never
    touches ~/.claude/settings.json. `cabildo codex` prints the config block and
    returns 0 when codex is not on PATH, so a missing Codex is a clear, non-fatal
    fallback rather than a failure.
    """
    if not CABILDO_BIN.exists():
        raise InstallError(f"cabildo is not installed in the venv: {CABILDO_BIN}")
    print("• wiring Codex (via `cabildo codex`)")
    sh([str(CABILDO_BIN), "codex"])


def install_opencode() -> None:
    """Wire the opencode client by delegating to the installed `cabildo opencode`,
    which registers the cabildo MCP server in opencode's JSON config (tagged
    opencode) and installs the /agenda + /sanity commands. Does not touch
    ~/.claude/settings.json.
    """
    print("• wiring opencode (via `cabildo opencode`)")
    sh([str(CABILDO_BIN), "opencode"])


def _codex_config():
    """Import cabildo.codex_config straight from the repo source, for building
    the (non-mutating) Codex preview without needing the venv to exist yet.
    """
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from cabildo import codex_config

    return codex_config


def _mods():
    """Import cabildo.mods from the repo source — the single owner of the mod's
    marketplace id and the `claude plugin` sequence, shared with the CLI verb.
    """
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from cabildo import mods

    return mods


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def preview(clients: tuple[str, ...]) -> None:
    """Print every action the real install would take, and change nothing."""
    print("cabildo installer — DRY RUN (--print): nothing will be changed\n")
    print(f"• venv: {VENV}")
    print(f"    $ uv venv --python 3.13 {VENV}   (if missing)")
    print(f"    $ uv pip install --python {VENV_PY} -e {REPO}")
    print("• data dir: migrate ~/.agent-chat -> ~/.cabildo if the old one exists")

    if "claude" in clients:
        print("\n• Claude: register the MCP server (user scope):")
        print(f"    $ claude mcp add --scope user cabildo -- {MCP_BIN}")
        print(f"• Claude: wire hooks into {SETTINGS} (existing entries preserved):")
        for event, (event_arg, *_rest) in HOOKS.items():
            print(f"    {event}: {hook_command(event_arg)}")
        print(f"    env.CABILDO_BIN = {CABILDO_BIN}")
        mods = _mods()
        if mods.mods_dir() is not None:
            print("• Claude: install the push-delivery mod (user scope):")
            for argv in mods.plan(scope="user"):
                print(f"    $ {' '.join(argv)}")

    if "codex" in clients:
        cc = _codex_config()
        user_cfg, proj_cfg = cc.config_paths()
        print("\n• Codex: register the MCP server:")
        print(f"    $ {' '.join(cc.add_argv())}")
        print(f"  (or paste into {user_cfg}, or {proj_cfg} for this repo only):")
        print(_indent(cc.toml_block()))
        print(f"• Codex: merge hooks into {cc.hooks_path()}:")
        print(_indent(cc.hooks_preview()))
        print("  Codex requires trusting hooks: run /hooks inside Codex afterwards.")
        if "claude" not in clients:
            print("  (~/.claude/settings.json is NOT touched for --client codex)")

    if "opencode" in clients:
        from cabildo import opencode_config as oc

        print(f"\n• opencode: register the MCP server in {oc.config_path()} (tagged opencode):")
        print(_indent(oc.preview()))
        print(f"• opencode: install /agenda + /sanity into {oc.command_dir()}")
        print("  (no hooks: opencode gets messaging, presence and advisory claims only)")


def _resolve_clients(client_arg: str) -> tuple[str, ...]:
    if client_arg == "both":
        return ("claude", "codex")
    if client_arg == "all":
        return ("claude", "codex", "opencode")
    return (client_arg,)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install cabildo for Claude Code and/or Codex.")
    ap.add_argument(
        "--client",
        choices=CLIENTS,
        default="claude",
        help="which client(s) to wire onto the bus (default: claude)",
    )
    ap.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show what would be done and change nothing",
    )
    args = ap.parse_args(argv)
    clients = _resolve_clients(args.client)

    if args.print_only:
        preview(clients)
        return 0

    try:
        print(f"cabildo installer  (repo: {REPO}; clients: {', '.join(clients)})\n")
        ensure_venv()
        migrate_legacy_home()
        if "claude" in clients:
            register_mcp()
            patch_settings()
            register_plugin()
        if "codex" in clients:
            install_codex()
        if "opencode" in clients:
            install_opencode()
    except InstallError as e:
        print(f"\n! {e}", file=sys.stderr)
        return 1

    extra = [c.capitalize() for c in ("codex", "opencode") if c in clients]
    restart = (" and " + " and ".join(extra)) if extra else ""
    print(
        f"\nDone. Restart any running Claude Code{restart} sessions so they pick up the "
        "new MCP server and hooks. New sessions will auto-join the bus."
    )
    if "codex" in clients:
        print("Codex only: run /hooks inside Codex to trust the hooks before they run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
