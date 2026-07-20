"""Process- and filesystem-level context resolution.

Two jobs live here, both deliberately dependency-free (stdlib only) so the
hooks can import them without pulling in the `mcp` package:

1. resolve_claude_pid() -- find the PID of the `claude` process that owns the
   current session. Both the hooks and the MCP subprocess are descendants of
   that single process, so it is the rendezvous key that lets the MCP server
   (which never sees the session_id) bind itself to the session_id that the
   hooks *do* see.

2. git_project() -- map a working directory to its git repo root, which is what
   we treat as a "project" for coordination between agents on the same code.
"""

from __future__ import annotations

import os
from pathlib import Path


# --------------------------------------------------------------------------- #
# Process-tree rendezvous (Linux /proc)
# --------------------------------------------------------------------------- #
def _ppid(pid: int) -> int:
    """Parent PID of `pid`, or 0. Parses /proc/<pid>/stat robustly: comm is
    wrapped in parens and may itself contain spaces/parens, so we index off the
    final ')' rather than splitting the whole line."""
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return 0
    rparen = data.rfind(b")")
    if rparen == -1:
        return 0
    fields = data[rparen + 2 :].split()
    # fields[0] = state, fields[1] = ppid
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def _argv(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]


# Shells are opaque wrappers (`bash -c '<anything>'`); never identify a process
# by what its -c string mentions. Interpreters are transparent: the script path
# in argv[1:] is what the process really is.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_INTERPRETERS = ("python", "node", "bun", "deno")


def _exe_id(argv: list[str]) -> str:
    """The token that says what program this is: argv[0], or the script path
    for interpreter-launched processes ('node .../claude-code/cli.js'). Shell
    wrappers return '' — a `bash -c` string quoting 'claude' is not claude."""
    if not argv:
        return ""
    base = os.path.basename(argv[0]).lower()
    if base in _SHELLS:
        return ""
    if base.startswith(_INTERPRETERS):
        for arg in argv[1:]:
            if not arg.startswith("-"):
                return arg.lower()
        return ""
    return argv[0].lower()


def resolve_claude_pid(start_pid: int | None = None) -> int:
    """Walk the ancestor chain from `start_pid` (default: this process) and
    return the PID of the nearest `claude` process. 0 if none found.

    Identification uses the *executable* only (argv[0], or the script path for
    interpreter-launched processes) — never the argument tail. A prompt like
    `claude -p 'talk on the cabildo bus'` must not disqualify its own claude
    (that bug bound every such agent to an outer session and misattributed
    everything it sent), and a `bash -c '... cabildo ...'` wrapper must not
    qualify as one. cabildo's own binaries are excluded so a hook or MCP
    process installed under a claude-ish path never matches itself."""
    pid = _ppid(start_pid if start_pid is not None else os.getpid())
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        exe = _exe_id(_argv(pid))
        if "claude" in exe and "cabildo" not in exe:
            return pid
        pid = _ppid(pid)
    return 0


# --------------------------------------------------------------------------- #
# Project detection
# --------------------------------------------------------------------------- #
def git_project(cwd: str | None = None) -> tuple[str, str]:
    """Return (project_root, project_name) for `cwd`.

    project_root is the nearest ancestor containing a .git entry; if there is no
    git repo we fall back to cwd itself. project_name is the basename."""
    start = Path(cwd or os.getcwd()).resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return str(d), d.name
    return str(start), start.name
