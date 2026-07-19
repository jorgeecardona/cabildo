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


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", "replace"
        )
    except OSError:
        return ""


def resolve_claude_pid(start_pid: int | None = None) -> int:
    """Walk the ancestor chain from `start_pid` (default: this process) and
    return the PID of the nearest `claude` process. 0 if none found.

    We match on the command line containing 'claude' while excluding our own
    backchat processes, so a hook launched via `bash -c` (bash -> claude) and
    the MCP server launched directly (python -> claude) both resolve to the same
    session-owning claude PID."""
    pid = _ppid(start_pid if start_pid is not None else os.getpid())
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        cl = _cmdline(pid).lower()
        if "claude" in cl and "backchat" not in cl and "backchat" not in cl:
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
