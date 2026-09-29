"""Read-only inventory of a repo's branches and worktrees, attributed to the
cabildo agents living in them.

Coding agents leave a zoo behind: a worktree per spawned task on its own
``agent/<role>/<stamp>`` branch, plus lanes and the main checkout, and over a few
days it is no longer obvious what any of them are or whose they are. This module
answers three questions per checkout — what is it, whose is it, and where does it
stand relative to the trunk — and nothing more. It is a pure inventory: it runs
only read-only git commands, never fetches (the ahead/behind is against the
*local* ``<remote>/<target>`` ref, so it is offline and instant), and changes
nothing on disk or on the bus.
"""

from __future__ import annotations

import subprocess
from typing import Any

_GIT_TIMEOUT = 15


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a git command read-only, returning (rc, stripped stdout+stderr). A
    missing directory or a git that cannot run is a non-zero rc, never a raise:
    an inventory of a broken worktree should still list the rest.
    """
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _parse_worktree_list(porcelain: str) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain`` into one dict per worktree with
    keys: path, head, branch (None if detached), bare, locked, prunable.
    """
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in porcelain.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {
                "path": val,
                "head": None,
                "branch": None,
                "bare": False,
                "locked": False,
                "prunable": False,
            }
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key == "detached":
            cur["branch"] = None
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
        elif key == "prunable":
            cur["prunable"] = True
    if cur:
        out.append(cur)
    return out


def _ahead_behind(root: str, ref: str, base: str) -> tuple[int | None, int | None]:
    """(ahead, behind) of ``ref`` relative to ``base``: commits in ref not base,
    and in base not ref. None when the comparison cannot be made (e.g. base ref
    absent because the repo has never fetched the remote).
    """
    rc, out = _git(["rev-list", "--left-right", "--count", f"{base}...{ref}"], root)
    if rc != 0 or "\t" not in out:
        return None, None
    behind_s, _, ahead_s = out.partition("\t")
    try:
        return int(ahead_s.strip()), int(behind_s.strip())
    except ValueError:
        return None, None


def _contained_in(root: str, ref: str, base: str) -> bool:
    """True if every commit of ``ref`` is already in ``base`` (ref is an ancestor
    of base). Note: a branch cabildo pushed by REBASE gets new commit shas on the
    trunk, so its old tip is not an ancestor and this reads False even though the
    work is on main — ahead/behind tells that fuller story.
    """
    return _git(["merge-base", "--is-ancestor", ref, base], root)[0] == 0


def _last_commit(root: str, ref: str) -> dict[str, Any] | None:
    rc, out = _git(["log", "-1", "--format=%ct%x00%s", ref], root)
    if rc != 0 or "\x00" not in out:
        return None
    ts, _, subject = out.partition("\x00")
    try:
        return {"at": int(ts), "subject": subject.strip()}
    except ValueError:
        return None


def _dirty_count(path: str) -> int | None:
    """Count of tracked, uncommitted changes in a worktree (untracked files are
    not counted — they block nothing). None if the tree cannot be read.
    """
    rc, out = _git(["status", "--porcelain"], path)
    if rc != 0:
        return None
    return sum(1 for ln in out.splitlines() if ln and not ln.startswith("??"))


def _agents_by_worktree_and_branch(conn: Any) -> tuple[dict[str, dict], dict[str, dict]]:
    """Two maps from the spawn ledger: worktree-path -> agent info, and
    branch-name -> agent info. Agent info is {name, status}. The ledger is the
    only place that ties a git worktree back to the agent that was launched in it.
    """
    from . import store

    by_wt: dict[str, dict] = {}
    by_branch: dict[str, dict] = {}
    for r in store.list_spawns(conn, limit=500):
        info = {"name": r.get("agent_display") or r.get("tag"), "status": r["status"]}
        if r.get("worktree"):
            by_wt[r["worktree"]] = info
        if r.get("branch"):
            by_branch[r["branch"]] = info
    return by_wt, by_branch


def _live_sessions_by_tree(conn: Any, project: str | None) -> dict[str, dict]:
    """tree-root -> {name, presence} for every currently active session, so a
    worktree an agent is standing in right now is shown as live regardless of what
    the (slower-moving) spawn ledger says.
    """
    from . import store
    from .context import tree_root

    out: dict[str, dict] = {}
    for s in store.active_sessions(conn, project=project):
        if not s["cwd"]:
            continue
        out[tree_root(s["cwd"])] = {
            "name": store.display_name(s),
            "presence": store.presence_state(s),
        }
    return out


def inventory(
    cwd: str | None = None, target: str = "main", remote: str = "origin"
) -> dict[str, Any]:
    """Inventory the repo containing ``cwd``: every worktree and every local
    branch not backed by one, each placed against ``<remote>/<target>`` and
    attributed to a cabildo agent when one is known. Read-only and offline.
    """
    import os

    from . import store
    from .context import git_project, tree_root

    start = cwd or os.getcwd()
    rc, root = _git(["rev-parse", "--show-toplevel"], start)
    if rc != 0:
        return {"ok": False, "reason": f"not a git repository: {start}"}
    root = root.strip()
    base = f"{remote}/{target}"
    base_present = _git(["rev-parse", "--verify", "--quiet", base], root)[0] == 0

    # git_project returns (root_path, name): sessions are filtered by the PATH
    # (the sessions.project column), while the name is only for display.
    project_root: str | None = root
    project: str | None = None
    try:
        project_root, project = git_project(root)
    except Exception:
        project_root, project = root, None

    conn = store.connect()
    try:
        agents_by_wt, agents_by_branch = _agents_by_worktree_and_branch(conn)
        live_by_tree = _live_sessions_by_tree(conn, project_root)
    finally:
        conn.close()

    rc, porcelain = _git(["worktree", "list", "--porcelain"], root)
    wt_raw = _parse_worktree_list(porcelain) if rc == 0 else []

    worktrees: list[dict[str, Any]] = []
    branches_with_worktree: set[str] = set()
    for w in wt_raw:
        branch = w["branch"]
        if branch:
            branches_with_worktree.add(branch)
        ref = branch or (w["head"] or "HEAD")
        ahead, behind = _ahead_behind(root, ref, base) if base_present else (None, None)
        entry = {
            "path": w["path"],
            "branch": branch,
            "head": (w["head"] or "")[:12],
            "detached": branch is None,
            "bare": w["bare"],
            "locked": w["locked"],
            "prunable": w["prunable"],
            "dirty": _dirty_count(w["path"]),
            "ahead": ahead,
            "behind": behind,
            "in_trunk": _contained_in(root, ref, base) if base_present else None,
            "last_commit": _last_commit(root, ref),
            "agent": (live_by_tree.get(tree_root(w["path"])) or agents_by_wt.get(w["path"])),
        }
        worktrees.append(entry)

    # Local branches with no worktree of their own — the ones easiest to forget,
    # since nothing is checked out on them.
    branches: list[dict[str, Any]] = []
    rc, refs = _git(
        ["for-each-ref", "--format=%(refname:short)%00%(objectname:short)", "refs/heads"], root
    )
    if rc == 0:
        for line in refs.splitlines():
            name, _, sha = line.partition("\x00")
            if not name or name in branches_with_worktree:
                continue
            ahead, behind = _ahead_behind(root, name, base) if base_present else (None, None)
            branches.append(
                {
                    "branch": name,
                    "head": sha,
                    "ahead": ahead,
                    "behind": behind,
                    "in_trunk": _contained_in(root, name, base) if base_present else None,
                    "last_commit": _last_commit(root, name),
                    "agent": agents_by_branch.get(name),
                }
            )

    return {
        "ok": True,
        "repo": root,
        "project": project,
        "base": base,
        "base_present": base_present,
        "worktrees": worktrees,
        "branches": branches,
    }
