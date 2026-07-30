"""Process- and filesystem-level context resolution.

Two jobs live here, both deliberately dependency-free (stdlib only) so the
hooks can import them without pulling in the `mcp` package:

1. resolve_claude_pid() -- find the PID of the `claude` process that owns the
   current session. Both the hooks and the MCP subprocess are descendants of
   that single process, so it is the rendezvous key that lets the MCP server
   (which never sees the session_id) bind itself to the session_id that the
   hooks *do* see.

2. git_project() -- map a working directory to a "project": the git repo root by
   default, or whatever `cabildo add` pinned that folder to. That pin is read
   from ~/.cabildo/cloud.json, which is the one thing here that isn't purely
   filesystem-derived — imported lazily so a missing/corrupt registry can never
   stop a session from resolving.
"""

from __future__ import annotations

import os
import re
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
def _worktree_main_root(git_file: Path) -> Path | None:
    """If `git_file` is a git-worktree marker (a regular `.git` *file* holding
    `gitdir: <path>`), return the main working tree's root; else None.

    A linked worktree's .git file points at <main>/.git/worktrees/<name>. That
    dir's `commondir` resolves to the main <main>/.git, whose parent is the repo
    root. Without this, every worktree looks like its own project and agents on
    the same repo land in separate channels (proj:mcp-oauth instead of
    proj:paas), unable to see each other."""
    try:
        text = git_file.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = (git_file.parent / gitdir).resolve()
    commondir_file = gitdir / "commondir"
    try:
        common = commondir_file.read_text().strip()
        common_git = common if os.path.isabs(common) else str((gitdir / common).resolve())
    except OSError:
        # No commondir (unexpected layout): fall back to .git/worktrees/<n> -> .git
        common_git = str(gitdir.parent.parent)
    return Path(common_git).parent


# Claude Code's per-session scratch dir:
#   /tmp/claude-<uid>/<cwd-with-slashes-as-dashes>/<session-id>/scratchpad/...
# An agent that cd's in there stops looking like a member of its project and
# starts looking like a project called "scratchpad" — wrong for coordination (it
# drops out of its team's channel) and wrong for privacy (the project's uplink
# no longer applies, so an opted-out folder leaks in under a different name).
_SCRATCH_ROOT = re.compile(r"^claude-\d+$")


def _unmangle(mangled: str) -> Path | None:
    """Turn `-home-jcardona-stuff-x-agent-chat` back into a real directory.

    The mangling is lossy: every '/' became '-', and directory names contain
    dashes too, so `-a-b` is both /a/b and /a-b. Resolve it against the actual
    filesystem, taking the longest segment that exists at each step — that is
    what distinguishes `.../projects/agent-chat` from a `.../projects/agent/chat`
    that was never there. Returns None if the path can't be rebuilt (the origin
    was deleted, or it never looked like this)."""
    tokens = [t for t in mangled.lstrip("-").split("-") if t]
    if not tokens:
        return None
    cur, i = Path("/"), 0
    while i < len(tokens):
        for j in range(len(tokens), i, -1):          # longest candidate first
            cand = cur / "-".join(tokens[i:j])
            if cand.is_dir():
                cur, i = cand, j
                break
        else:
            return None
    return cur


def scratchpad_origin(path: Path) -> Path | None:
    """The working directory a scratchpad belongs to, or None if `path` isn't one."""
    parts = path.parts
    for k, part in enumerate(parts):
        if _SCRATCH_ROOT.match(part) and k + 1 < len(parts):
            return _unmangle(parts[k + 1])
    return None


def _git_project(start: Path) -> tuple[str, str]:
    """The guess: nearest ancestor containing a .git entry, named after its
    directory. No git repo anywhere above → the directory itself. A linked
    worktree resolves to its main repo so agents on the same codebase share one
    project."""
    for d in (start, *start.parents):
        dot_git = d / ".git"
        if dot_git.is_dir():
            return str(d), d.name
        if dot_git.is_file():
            main_root = _worktree_main_root(dot_git)
            if main_root is not None:
                return str(main_root), main_root.name
            return str(d), d.name
    return str(start), start.name


def git_project(cwd: str | None = None) -> tuple[str, str]:
    """Return (project_root, project_name) for `cwd`.

    The git repo is the default answer, but `cabildo add --project <name> <dir>`
    can pin a folder to a name, and that pin wins — it is an explicit statement
    about what this code IS, against a guess made from a directory name.

    Where the pin sits decides how far it reaches:

      * at the git root       → renames the repo.
      * below the git root    → that folder becomes its own project, root and
                                all (one package of a monorepo).
      * ABOVE a nested repo   → ignored. A repo inside a pinned folder stays its
                                own project.

    That last rule is not a detail. Jorge's journal repo holds ~/…/self with its
    own git, and ~/…/self/projects/<repo> inside it: pinning the parent must not
    quietly adopt twenty nested repos, because the pin carries an uplink level
    with it — the inheriting direction of that mistake pushes private
    conversations to the cloud. A nested .git is a stronger statement of "this is
    a separate thing" than an ancestor folder's pin. Pin the child explicitly
    (same name) if you really want it folded in.

    A session sitting in its Claude Code scratchpad resolves to the project the
    scratchpad belongs to, not to a project called "scratchpad".

    CABILDO_PROJECT overrides everything, for one-off runs and for `.mcp.json`
    entries written by `cabildo add`."""
    start = Path(cwd or os.getcwd()).resolve()
    origin = scratchpad_origin(start)
    if origin is not None:
        start = origin
    root, name = _git_project(start)

    try:
        from . import cloud
        pinned = cloud.project_for_path(start) or cloud.project_for_path(root)
    except Exception:      # a corrupt/unreadable registry must not break sessions
        pinned = None
    if pinned:
        pinned_name, pinned_root = pinned
        if len(pinned_root) >= len(root):      # at, or below, the repo it names
            name = pinned_name
            if len(pinned_root) > len(root):
                root = pinned_root

    override = (os.environ.get("CABILDO_PROJECT") or "").strip()
    return root, (override.lower() if override else name)
