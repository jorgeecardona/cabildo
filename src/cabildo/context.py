"""Process- and filesystem-level context resolution.

Two jobs live here, both deliberately dependency-free (stdlib only) so the
hooks can import them without pulling in the `mcp` package:

1. resolve_claude_pid() -- find the PID of the `claude` process that owns the
   current session. Both the hooks and the MCP subprocess are descendants of
   that single process, so it is the rendezvous key that lets the MCP server
   (which never sees the session_id) bind itself to the session_id that the
   hooks *do* see. The walk itself (`_ppid`/`_argv`, `_is_claude`, `_exe_id`)
   is platform-agnostic; only the two leaf reads differ: Linux parses
   `/proc/<pid>/{stat,cmdline}` directly (the fast path, and the one under
   test on every CI run), macOS -- which has no /proc -- shells out to `ps`
   instead (`_ppid_darwin`/`_argv_darwin`). Both feed the same `_is_claude`
   check, so identity rules (never trust a shell's `-c` argument, never treat
   an interpreter's own path as the script it's running) apply identically on
   either OS.

2. git_project() -- map a working directory to a "project": the git repo root by
   default, or whatever `cabildo add` pinned that folder to. That pin is read
   from ~/.cabildo/cloud.json, which is the one thing here that isn't purely
   filesystem-derived — imported lazily so a missing/corrupt registry can never
   stop a session from resolving.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# Process-tree rendezvous -- Linux /proc fast path, `ps`-backed fallback
# elsewhere (macOS has no /proc at all).
# --------------------------------------------------------------------------- #
def _ppid_linux(pid: int) -> int:
    """Parent PID of `pid`, or 0. Parses /proc/<pid>/stat robustly: comm is
    wrapped in parens and may itself contain spaces/parens, so we index off the
    final ')' rather than splitting the whole line.
    """
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


def _argv_linux(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]


def _ps(*args: str) -> str:
    """Run `ps <args>` and return stripped stdout, or '' on any failure.

    The one primitive the darwin backend is built on: no /proc there, but `ps`
    is always present and needs no extra dependency (this package stays
    stdlib-only so the hooks can import it without pulling in `mcp`). A pid
    that no longer exists makes `ps` exit non-zero with empty output on macOS
    -- indistinguishable here from "ps itself is broken" or "sandboxed away
    from us", and all three must degrade to "no information", never raise.
    Same failure contract as the /proc reads above: caller sees an empty
    result, not an exception.
    """
    try:
        proc = subprocess.run(["ps", *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _ppid_darwin(pid: int) -> int:
    """Parent PID via `ps -o ppid=`, macOS's only portable source for it."""
    try:
        return int(_ps("-o", "ppid=", "-p", str(pid)))
    except ValueError:
        return 0


def _argv_darwin(pid: int) -> list[str]:
    """Best-effort argv via `ps -ww -o command=`.

    `-ww` disables ps's column-width truncation -- without it a long
    interpreter/wrapper command line (exactly the `node .../cli.js` shape
    `_exe_id` needs to see the tail of) gets cut off and silently
    misidentified. Unlike /proc/<pid>/cmdline this is NOT NUL-delimited: ps
    reconstructs one space-joined string with no quoting, so an argument that
    itself contains a space is indistinguishable from two arguments. That is
    an inherent limit of `ps` (no psutil here), and it does not matter for our
    one consumer -- `_exe_id` only ever looks at argv[0], or (for interpreters)
    the first non-flag token in argv[1:], both of which land correctly
    regardless of how anything *after* them got split.
    """
    text = _ps("-ww", "-o", "command=", "-p", str(pid))
    return text.split() if text else []


def _ppid(pid: int) -> int:
    return _ppid_darwin(pid) if sys.platform == "darwin" else _ppid_linux(pid)


def _argv(pid: int) -> list[str]:
    return _argv_darwin(pid) if sys.platform == "darwin" else _argv_linux(pid)


# Shells are opaque wrappers (`bash -c '<anything>'`); never identify a process
# by what its -c string mentions. Interpreters are transparent: the script path
# in argv[1:] is what the process really is.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_INTERPRETERS = ("python", "node", "bun", "deno")


def _exe_id(argv: list[str]) -> str:
    """The token that says what program this is: argv[0], or the script path
    for interpreter-launched processes ('node .../claude-code/cli.js'). Shell
    wrappers return '' — a `bash -c` string quoting 'claude' is not claude.
    """
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


def _exe_is(pid: int, needle: str) -> bool:
    """True if `pid`'s executable identifies as `needle` (and isn't cabildo's own
    binary). One predicate behind both the claude and codex walks.
    """
    exe = _exe_id(_argv(pid))
    return needle in exe and "cabildo" not in exe


def _is_claude(pid: int) -> bool:
    return _exe_is(pid, "claude")


def _is_codex(pid: int) -> bool:
    return _exe_is(pid, "codex")


def _resolve_agent_pid(is_match, start_pid: int | None, include_self: bool) -> int:
    """Nearest ancestor (optionally self) whose exe matches `is_match`, or 0.

    The shared walk behind resolve_claude_pid/resolve_codex_pid: exe-only
    identification, nothing cached, a dead/unreadable pid ends the walk rather
    than guessing. See resolve_claude_pid for the full rationale.
    """
    start = start_pid if start_pid is not None else os.getpid()
    if include_self and start > 1 and is_match(start):
        return start
    pid = _ppid(start)
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        if is_match(pid):
            return pid
        pid = _ppid(pid)
    return 0


def resolve_codex_pid(start_pid: int | None = None, *, include_self: bool = False) -> int:
    """Nearest `codex` ancestor pid, 0 if none. The Codex counterpart of
    resolve_claude_pid: the codex process a Codex hook and the cabildo MCP
    server both descend from, used as the owner-pid rendezvous anchor so a Codex
    session's real id (bound by its SessionStart hook) and its MCP server agree.
    """
    return _resolve_agent_pid(_is_codex, start_pid, include_self)


def resolve_claude_pid(start_pid: int | None = None, *, include_self: bool = False) -> int:
    """Walk the ancestor chain from `start_pid` (default: this process) and
    return the PID of the nearest `claude` process. 0 if none found.

    Identification uses the *executable* only (argv[0], or the script path for
    interpreter-launched processes) — never the argument tail. A prompt like
    `claude -p 'talk on the cabildo bus'` must not disqualify its own claude
    (that bug bound every such agent to an outer session and misattributed
    everything it sent), and a `bash -c '... cabildo ...'` wrapper must not
    qualify as one. cabildo's own binaries are excluded so a hook or MCP
    process installed under a claude-ish path never matches itself.

    `include_self` starts the walk at `start_pid` itself. Hooks and the MCP
    server want the default (they are never claude), but memory accounting walks
    *arbitrary* pids up to their owning agent, and there claude's own resident
    set belongs to claude.

    Conservative by construction, on both backends: nothing here is cached
    across calls, so every `_ppid`/`_is_claude` check reflects the process
    table *right now*. A pid that has exited (or that `ps`/`/proc` can't read)
    makes `_ppid` return 0, which ends the walk with "no claude found" rather
    than a guess -- it never carries forward a pid-to-identity mapping that a
    just-exited process's slot could hand to some unrelated reused pid. That
    also means an intermediate pid getting reused mid-walk by an unrelated
    process can at worst end the walk early (that process is checked against
    `_is_claude` like any other and almost certainly fails); it can never merge
    two sessions or attribute a live process to the wrong agent, because
    identity here is exe-only (see `_exe_id`) and is never inherited from a
    pid's *previous* occupant.
    """
    return _resolve_agent_pid(_is_claude, start_pid, include_self)


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
    proj:paas), unable to see each other.
    """
    try:
        text = git_file.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
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
    was deleted, or it never looked like this).
    """
    tokens = [t for t in mangled.lstrip("-").split("-") if t]
    if not tokens:
        return None
    cur, i = Path("/"), 0
    while i < len(tokens):
        for j in range(len(tokens), i, -1):  # longest candidate first
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


# The session_id sits one level below the mangled cwd in the same path. It is
# the only thing that still names a dead agent: once its `claude` exits, process
# ancestry says nothing, but a venv or worktree it built under its scratchpad
# carries the id in every child's argv forever.
_SCRATCH_SESSION = re.compile(
    r"claude-\d+/[^/\s]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def scratchpad_session(text: str) -> str | None:
    """The session_id named by a scratchpad path appearing anywhere in `text`
    (a path, or a whole command line), or None.
    """
    m = _SCRATCH_SESSION.search(text or "")
    return m.group(1) if m else None


def _git_project(start: Path) -> tuple[str, str]:
    """The guess: nearest ancestor containing a .git entry, named after its
    directory. No git repo anywhere above → the directory itself. A linked
    worktree resolves to its main repo so agents on the same codebase share one
    project.
    """
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
    entries written by `cabildo add`.
    """
    start = Path(cwd or os.getcwd()).resolve()
    origin = scratchpad_origin(start)
    if origin is not None:
        start = origin
    root, name = _git_project(start)

    try:
        from . import cloud

        pinned = cloud.project_for_path(start) or cloud.project_for_path(root)
    except Exception:  # a corrupt/unreadable registry must not break sessions
        pinned = None
    if pinned:
        pinned_name, pinned_root = pinned
        if len(pinned_root) >= len(root):  # at, or below, the repo it names
            name = pinned_name
            if len(pinned_root) > len(root):
                root = pinned_root

    override = (os.environ.get("CABILDO_PROJECT") or "").strip()
    return root, (override.lower() if override else name)


def tree_root(cwd: str | None = None) -> str:
    """The working tree *containing* `cwd`: a linked worktree's own root, not
    the repo it belongs to.

    This is the opposite question from git_project(), which deliberately
    collapses worktrees onto their main repo so agents on one codebase share a
    channel. Here the collapse would be wrong: two agents in separate worktrees
    have separate indexes and stage only their own files, while two agents in
    one checkout stage each other's half-written work. Only the second is a
    collision, and only this function can tell them apart.
    """
    start = Path(cwd or os.getcwd()).resolve()
    origin = scratchpad_origin(start)
    if origin is not None:
        start = origin
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return str(d)
    return str(start)
