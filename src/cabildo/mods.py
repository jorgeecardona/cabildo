"""Install the bundled Claude Code mod via the `claude plugin` CLI.

The mod — push delivery, GitLab #30 — lives in the repo's `mods/` marketplace
root, outside the Python package. Both the one-shot installer
(`scripts/install.py`) and the `cabildo plugin` CLI verb install it, so the
command sequence lives here once: validate the manifest, then
marketplace-add / install / enable (and update, so re-running after editing the
mod refreshes the snapshot). Installing is non-fatal by contract — a `claude`
without plugin subcommands, or a failed validate, returns a Result saying why
rather than raising: the bus works without the mod; it only adds the idle-wake
doorbell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MARKETPLACE = "cabildo-mods"
PLUGIN = "cabildo-push"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"

# A runner echoes and runs one argv, returning its exit code. It is injected so
# the installer and the CLI each render the command in their own style while the
# sequence and the claude-detection stay here.
Runner = Callable[[list[str]], int]

# This package's own directory (…/cabildo). The bundled mod sits beside it in a
# wheel (`_bundled_mods/`) or two levels up in a repo checkout (`../mods`).
_PKG_DIR = Path(__file__).resolve().parent


def mods_dir() -> Path | None:
    """The bundled marketplace root, or None when it cannot be found.

    Checked in order: `CABILDO_MODS_DIR` (an explicit override); the copy shipped
    inside the package at `cabildo/_bundled_mods/` (a plain wheel install in
    site-packages — the common case for `pip`/`uv pip install cabildo`); then the
    repo's own `mods/` two parents up (an editable checkout, where the wheel's
    force-include has not run). First one that exists wins; None otherwise, and
    the caller reports there is nothing to install rather than guessing a path.
    """
    override = os.environ.get("CABILDO_MODS_DIR")
    if override:
        p = Path(override).expanduser()
        return p if p.exists() else None
    for cand in (_PKG_DIR / "_bundled_mods", _PKG_DIR.parents[1] / "mods"):
        if cand.exists():
            return cand
    return None


@dataclass(frozen=True)
class Step:
    """One `claude plugin` invocation and the exit code it returned."""

    argv: tuple[str, ...]
    rc: int


@dataclass(frozen=True)
class Result:
    """What a run did. `ok` is False when nothing useful happened (no `claude`,
    no bundled mod, or a failed validate); `reason` says which. `steps` is every
    command actually run, in order, each with its exit code — the caller reports
    them.
    """

    ok: bool
    reason: str | None = None
    steps: tuple[Step, ...] = field(default_factory=tuple)


def _run_echo(argv: list[str]) -> int:
    """Default runner: print the command, run it, return its exit code."""
    import shlex

    print("  $", " ".join(shlex.quote(c) for c in argv))
    return subprocess.call(argv)


def install(run: Runner = _run_echo, *, scope: str = "user", update: bool = True) -> Result:
    """Validate the mod, then install and enable it at `scope` (default user).

    `validate` gates the rest: a manifest the engine would refuse is reported
    and nothing is installed. The remaining steps are idempotent in practice (a
    second run reports 'already …'); their exit codes ride along in the Result
    but do not abort the sequence.
    """
    d = mods_dir()
    if d is None:
        return Result(ok=False, reason="no bundled mod found (set CABILDO_MODS_DIR to override)")
    if shutil.which("claude") is None:
        return Result(ok=False, reason="`claude` is not on PATH")

    steps: list[Step] = []

    def do(argv: list[str]) -> int:
        rc = run(argv)
        steps.append(Step(argv=tuple(argv), rc=rc))
        return rc

    if do(["claude", "plugin", "validate", str(d)]) != 0:
        return Result(ok=False, reason="`claude plugin validate` failed", steps=tuple(steps))

    do(["claude", "plugin", "marketplace", "add", str(d)])
    do(["claude", "plugin", "install", PLUGIN_ID, "--scope", scope])
    do(["claude", "plugin", "enable", PLUGIN_ID, "--scope", scope])
    if update:
        do(["claude", "plugin", "update", PLUGIN_ID])
    return Result(ok=True, steps=tuple(steps))


def remove(run: Runner = _run_echo, *, scope: str = "user") -> Result:
    """Uninstall the mod at `scope`. `uninstall` also drops the enable record, so
    there is no separate disable step. `ok` reflects the command's exit code — a
    `claude` too old for `uninstall` passes `shutil.which` but exits non-zero.
    """
    if shutil.which("claude") is None:
        return Result(ok=False, reason="`claude` is not on PATH")
    argv = ["claude", "plugin", "uninstall", PLUGIN_ID, "--scope", scope]
    rc = run(argv)
    reason = None if rc == 0 else f"`claude plugin uninstall` exited {rc}"
    return Result(ok=rc == 0, reason=reason, steps=(Step(argv=tuple(argv), rc=rc),))


def plan(scope: str = "user", *, update: bool = True, remove: bool = False) -> list[list[str]]:
    """The commands a real run would execute, for a dry-run preview. Changes
    nothing; mirrors `install`/`remove` so the preview cannot drift from them.
    """
    if remove:
        return [["claude", "plugin", "uninstall", PLUGIN_ID, "--scope", scope]]
    d = mods_dir()
    root = str(d) if d is not None else "<no mods dir found>"
    steps = [
        ["claude", "plugin", "validate", root],
        ["claude", "plugin", "marketplace", "add", root],
        ["claude", "plugin", "install", PLUGIN_ID, "--scope", scope],
        ["claude", "plugin", "enable", PLUGIN_ID, "--scope", scope],
    ]
    if update:
        steps.append(["claude", "plugin", "update", PLUGIN_ID])
    return steps
