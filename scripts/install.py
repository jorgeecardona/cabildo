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
    the MCP server in opencode's JSON config (tagged opencode). opencode has no
    cabildo hook adapter yet, so it gets messaging, presence and advisory claims,
    not hook-enforced leases.

Choose clients with `--client {claude,codex,opencode,both,all}` (default: claude,
which preserves the historical behavior; `both` = claude+codex, `all` = every
client). Idempotent: re-running won't duplicate hooks or the MCP entry. Pass
`--print` to show what it would do and change nothing — no venv, no package
install, no data migration, no registration, no hook writes.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))  # the shared wiring lives in the package
from cabildo import install as shared

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

HOOKS = shared.HOOKS


InstallError = shared.InstallError


require = shared.require
sh = shared.sh


def hook_command(event_arg: str) -> str:
    """The shell string a Claude hook stores as its ``command`` (see ``shared.hook_command``)."""
    return shared.hook_command(HOOK_BIN, event_arg)


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
    shared.register_mcp(MCP_BIN)


def patch_settings() -> None:
    shared.patch_settings(SETTINGS, HOOK_BIN, CABILDO_BIN)


def register_plugin() -> None:
    shared.register_plugin()


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
    which registers the cabildo MCP server in opencode's JSON config, tagged
    opencode. Does not touch ~/.claude/settings.json.
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
        shared.preview_claude(SETTINGS, MCP_BIN, HOOK_BIN, CABILDO_BIN, print)

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
