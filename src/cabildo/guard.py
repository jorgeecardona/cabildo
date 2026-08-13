"""Decision logic for the PreToolUse guard hook: turn advisory claims into
enforced ones at the tool-call boundary.

Four conventions are enforced, all scoped to the session's own project:

  * ``file:<path>`` — an Edit/Write/NotebookEdit on a matching path is denied.
    Paths are matched repo-relative and absolute, with fnmatch globbing so a
    claim like ``file:cabildo/hooks/*`` covers the directory. And, crucially,
    an *allowed* edit auto-takes a short lease on that path (``autoclaim_on_edit``)
    so the guard has something to enforce without any agent remembering to
    claim — the first agent to touch a file owns it briefly; the next is denied.
  * ``git push`` — denied while ``push:<project_name>`` is held by someone else,
    and *also* denied while any other agent is simply live on the project, with
    the reason pointing at ``request land``. A lease you hold yourself is the
    deliberate escape hatch.
  * blanket staging in a shared working tree — ``git add -A``, ``git add .``,
    ``git commit -a`` — is denied while another agent shares the same checkout,
    because it sweeps their half-written files into your commit.
  * destructive git in a shared working tree — ``git reset --hard``, a
    discarding ``checkout``/``restore``, ``clean -f``, ``stash`` — is denied
    while any *other* agent is active on the project, because those wipe the
    whole tree's uncommitted work, not just one claimed file.

The last three are one story: concurrent agents on one repo collide at commit
and push, and every recovery from that collision (reset, revert, force-push)
destroys more than the collision did. So the guard stops the collision instead
of the recovery, and every deny names the ordered path through — ``land``, which
the daemon runs one at a time.

Deny only ever fires against a live peer or a claim held by *someone else*;
everything else falls through to Claude Code's normal permission flow. The reason
string names the holder/peers so the denied agent can go negotiate on the bus
instead of retrying blind.
"""

from __future__ import annotations

import os
import shlex
import sqlite3
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

from . import store
from .context import tree_root


# Working-tree-destroying git ops. Each matches only as its own shell command
# (start or after ;&|), so the same words inside a quoted string don't trip it.
# These clobber *everyone's* uncommitted work in a shared checkout, which is the
# concrete harm we saw ("the reset that dropped feaf332a").
# Working-tree-destroying git, matched on TOKENS like every other check here.
#
# These were regexes over the raw command string, and `git\s+reset` cannot see
# past git's own options: `git -C /path reset --hard` matched nothing and walked
# straight through the guard. Tokenising also gets the easy cases free — a
# subcommand name inside a quoted string is one token, not a command.
def _discards_reset(args: list[str]) -> bool:
    return "--hard" in args


def _discards_checkout(args: list[str]) -> bool:
    # `--` introduces paths, `.` is the whole tree; both discard. A ref alone
    # switches branches, which is a different denial with a different reason.
    return "--" in args or "." in args


def _discards_restore(args: list[str]) -> bool:
    # restore discards the working tree unless it is only touching the index.
    return not any(a == "--staged" or a.startswith("--staged=") for a in args)


def _discards_clean(args: list[str]) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and "f" in a for a in args) or (
        "--force" in args
    )


def _discards_stash(args: list[str]) -> bool:
    # `stash list/show/pop/apply` read or restore; bare `stash` and `stash push`
    # take the tree away.
    return not args or args[0] not in ("list", "show", "pop", "apply", "drop", "branch")


_DESTRUCTIVE_GIT = {
    "reset": (_discards_reset, "git reset --hard"),
    "checkout": (_discards_checkout, "git checkout (discard)"),
    "restore": (_discards_restore, "git restore (discard)"),
    "clean": (_discards_clean, "git clean -f"),
    "stash": (_discards_stash, "git stash"),
}


def _destructive(argv: list[str]) -> str | None:
    """The label for a command that would discard this checkout's work, else None."""
    sub, args = _git_subcommand(argv)
    entry = _DESTRUCTIVE_GIT.get(sub or "")
    if entry is None:
        return None
    predicate, label = entry
    return label if predicate(args) else None


# Staging the whole tree rather than named paths. Matched on *tokens*, not on
# the raw string: `git commit -m "add -A support"` must not read as `git add -A`,
# and a message is one token while a flag is another.
_STAGE_ALL = {"-A", "--all", "-u", "--update", "--no-ignore-removal", ".", ":/"}
# git's own options that swallow the next token, so the subcommand is found past
# `git -C /repo add`.
_GIT_OPT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

_FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}

# How long an auto-claim taken on edit lasts. Short, and refreshed on every
# re-edit, so an agent that moves on releases the file within minutes without
# needing to remember; SessionEnd drops it immediately.
AUTOCLAIM_TTL = 600.0


def _argvs(command: str) -> list[list[str]]:
    """A shell string split into one argv per command.

    Tokenising is what keeps `git commit -m "add -A support"` out of the
    blanket-stage check: the message is a single token, and a token is not a
    flag. Unparseable input yields nothing — this guard catches the ordinary
    case, it does not try to out-parse bash."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:                    # unbalanced quotes; not ours to fix
        return []
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in (";", "&&", "||", "|", "&"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _git_subcommand(argv: list[str]) -> tuple[str | None, list[str]]:
    """(subcommand, its arguments) for a git argv, skipping git's own options."""
    if not argv or os.path.basename(argv[0]) != "git":
        return None, []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _GIT_OPT_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok, argv[i + 1:]
    return None, []


def _chain_cwd(command: str, session_cwd: str | None) -> str | None:
    """Where the commands in this chain actually run.

    The session's registered cwd is where the AGENT started, which is not where
    its commands run. An agent told to "isolate yourself first (`git worktree
    add`)" then works with `cd ../lane && …` from that same session — so judging
    it by the session cwd makes the prescribed remedy impossible to satisfy, and
    the checks below fire forever on a checkout no peer can even see.

    Handles the two shapes that occur in practice: a `cd <path>` in the chain,
    and `git -C <path>` on the git command itself (see _argv_cwd). Anything else
    falls back to the session cwd, which keeps the guard fail-safe — an unparsed
    command is judged as if it ran where the agent lives.
    """
    cwd = session_cwd
    for argv in _argvs(command):
        if argv and os.path.basename(argv[0]) == "cd" and len(argv) > 1:
            cwd = _resolved_dir(cwd, argv[1]) or cwd
    return cwd


def _resolved_dir(base: str | None, target: str) -> str | None:
    """`target` as an absolute directory, or None if it is not one on disk.

    Existence is deliberately the gate. This function only ever LOOSENS a
    denial, so a path we cannot verify must not be taken at its word: a typo, a
    variable that did not expand, or a directory that has not been created yet
    would otherwise read as "somewhere else" and wave the command through. Fall
    back to the agent's own cwd and let the guard fire.
    """
    path = os.path.normpath(os.path.join(base, target)) if base else target
    return path if os.path.isdir(path) else None


def _argv_cwd(argv: list[str], chain_cwd: str | None) -> str | None:
    """`git -C <path>` moves one command without moving the chain."""
    if not argv or os.path.basename(argv[0]) != "git":
        return chain_cwd
    for i, tok in enumerate(argv[:-1]):
        if tok == "-C":
            return _resolved_dir(chain_cwd, argv[i + 1]) or chain_cwd
    return chain_cwd


def _is_push(command: str) -> bool:
    """True if `command` runs `git push` — the subcommand, not the word.

    Tokenised for the same reason as staging, plus one this file learned the
    hard way: `git stash push -m wip` contains "push" and is its opposite. A
    pattern that reads the string mislabels it; reading the subcommand cannot."""
    return any(_git_subcommand(argv)[0] == "push" for argv in _argvs(command))


def _stages_whole_tree(argv: list[str]) -> str | None:
    """Label for a command that stages or commits the *whole* tree instead of
    named paths, else None. `git add src/x.py` is fine; `git add -A` is not."""
    sub, rest = _git_subcommand(argv)
    if sub == "add":
        return next((f"git add {t}" for t in rest if t in _STAGE_ALL), None)
    if sub == "commit":
        # -a, and short bundles containing it (-am). `--amend` is two dashes and
        # is not `--all`, so it falls through — amending is not staging.
        for tok in rest:
            if tok == "--all" or (
                tok.startswith("-") and not tok.startswith("--") and "a" in tok[1:]
            ):
                return f"git commit {tok}"
    return None


def _held_by_other(
    conn: sqlite3.Connection, resource: str, session_id: str
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM claims WHERE resource=? AND expires_at>? AND holder_session!=?",
        (resource, store._now(), session_id),
    ).fetchone()
    return row


def _holder_name(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    return store.display_name(
        store.get_session(conn, row["holder_session"]), row["holder_session"]
    )


def _held_by_me(conn: sqlite3.Connection, resource: str, session_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM claims WHERE resource=? AND expires_at>? AND holder_session=?",
        (resource, store._now(), session_id),
    ).fetchone() is not None


def _deny_push(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
    if not _is_push(command):
        return None
    resource = f"push:{me['project_name']}"
    row = _held_by_other(conn, resource, me["session_id"])
    if row:
        note = f" ({row['note']})" if row["note"] else ""
        return (
            f"cabildo: {_holder_name(conn, row)} holds the lock `{resource}`{note}, "
            f"expiring in {round(row['expires_at'] - store._now())}s. Pushing now could "
            "clobber their in-flight work. Wait, or message them via `send`; take the "
            "lock with `claim` once it is free."
        )
    # Nobody else holds the lock — but an unlocked push is still a race the
    # moment another agent is live here. Two agents that both fetched the same
    # tip both believe they are fast-forwarding; the loser then reaches for
    # rebase, revert or force-push, and *that* is what eats commits. The daemon
    # already runs one intent at a time, which is the ordering nobody can get
    # right by hand, so send them there rather than letting them find out.
    if _held_by_me(conn, resource, me["session_id"]):
        return None                       # you took the lock: peers are the ones denied
    peers = store.active_sessions(conn, project=me["project"], exclude=me["session_id"])
    if not peers:
        return None                       # alone: push is nobody's business but yours
    who = ", ".join(store.display_name(p) for p in peers[:5])
    return (
        f"cabildo: {len(peers)} other agent(s) live on {me['project_name']} ({who}) — "
        "a direct push races them. Queue it instead; the daemon fetches, rebases "
        "onto the target, runs the repo's gate, pushes, and verifies against the "
        "remote, one intent at a time:\n"
        f'    request(action="land", params={{"cwd": "{me["cwd"] or me["project"]}", '
        '"target": "<branch you are landing on, e.g. main>"})\n'
        "It lands commits and never decides what to commit, so commit first and "
        "leave no tracked changes behind. It DMs you the result. If you really "
        f"must push by hand, take `{resource}` with `claim` first — that denies "
        "the others and makes the rebase yours to own."
    )


def _deny_blanket_stage(
    conn: sqlite3.Connection, me: sqlite3.Row, command: str
) -> str | None:
    """Deny `git add -A` / `git commit -a` while another agent shares this exact
    working tree.

    Staging is not dangerous; staging *indiscriminately* is. In a shared
    checkout it sweeps whatever the other agent has half-written into your
    commit, and the fix for that — reset, revert, force-push — destroys more
    than the mistake did. Separate worktrees have separate indexes, so this
    never fires on agents who isolated themselves properly."""
    label = None
    for argv in _argvs(command):
        label = _stages_whole_tree(argv)
        if label:
            break
    if label is None:
        return None
    # cwd is what says *which checkout*; project can't (it collapses worktrees).
    # A row without one tells us nothing, so it is not treated as a sharer.
    if not me["cwd"]:
        return None
    mine = tree_root(_chain_cwd(command, me["cwd"]))
    sharers = [
        p for p in store.active_sessions(conn, project=me["project"],
                                         exclude=me["session_id"])
        if p["cwd"] and tree_root(p["cwd"]) == mine
    ]
    if not sharers:
        return None
    who = ", ".join(store.display_name(p) for p in sharers[:5])
    return (
        f"cabildo: `{label}` in a working tree shared with {len(sharers)} other "
        f"agent(s) ({who}) commits their half-written files as if they were "
        "yours. Stage the paths you actually changed (`git add <path> ...`), or "
        "isolate yourself first (`git worktree add`)."
    )


def _switches_branch(argv: list[str]) -> str | None:
    """The label for a command that moves *this checkout's* HEAD, or None.

    `checkout`/`switch` with a path restriction is a discard, not a switch — that is a different
    denial with a different reason, and it is already handled above. `git branch -f/-M` is included
    because moving a branch label that somebody else has checked out is the same accident wearing
    different clothes.
    """
    sub, args = _git_subcommand(argv)
    if sub in ("checkout", "switch"):
        if "--" in args:
            return None                     # `git checkout <tree-ish> -- <path>`: a restore
        if any(a in ("-b", "-B", "-c", "-C") for a in args):
            return f"git {sub} -b"
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            return None                     # bare `git checkout`: prints, changes nothing
        if sub == "checkout" and any(os.sep in a or os.path.exists(a) for a in positional):
            return None                     # looks like a path, not a ref
        return f"git {sub}"
    if sub == "branch" and any(a in ("-f", "--force", "-M", "-m") for a in args):
        return "git branch -f"
    return None


def _deny_branch_switch(
    conn: sqlite3.Connection, me: sqlite3.Row, command: str
) -> str | None:
    """Deny changing the branch while another agent shares this exact working tree.

    A checkout has ONE HEAD. Switching it moves every agent standing in that directory, and none of
    them are told — so the next one commits onto a branch it never chose, believing it is still on
    the one it started from. That is not hypothetical: it happened three times in this repo's own
    fleet on 2026-08-03, and in one case an agent landed three commits of unrelated work onto
    somebody else's feature branch before anyone noticed.

    Worktrees have their own HEAD and are never affected, which is also the escape hatch: the answer
    to "I need a branch" in a shared tree is a worktree, not a switch.
    """
    label = None
    for argv in _argvs(command):
        label = _switches_branch(argv)
        if label:
            break
    if label is None or not me["cwd"]:
        return None
    mine = tree_root(_chain_cwd(command, me["cwd"]))
    sharers = [
        p for p in store.active_sessions(conn, project=me["project"],
                                         exclude=me["session_id"])
        if p["cwd"] and tree_root(p["cwd"]) == mine
    ]
    if not sharers:
        return None
    who = ", ".join(store.display_name(p) for p in sharers[:5])
    return (
        f"cabildo: `{label}` moves the branch under {len(sharers)} other agent(s) "
        f"({who}) standing in this same checkout — they are not told, and the next "
        f"one commits onto a branch it never chose. Work in your own tree instead: "
        f"`git worktree add ../wt-<name> -b <name>`. If you genuinely need this "
        f"checkout switched, say so on the bus first and let them move."
    )


def _deny_file(conn: sqlite3.Connection, me: sqlite3.Row, path: str) -> str | None:
    abspath = PurePosixPath(path)
    rel: str | None = None
    project = me["project"]
    if project and str(abspath).startswith(project.rstrip("/") + "/"):
        rel = str(abspath)[len(project.rstrip("/")) + 1:]
    for row in conn.execute(
        "SELECT * FROM claims WHERE resource LIKE 'file:%' AND expires_at>? "
        "AND holder_session!=? AND project=?",
        (store._now(), me["session_id"], project),
    ):
        pattern = row["resource"][len("file:"):]
        if fnmatch(str(abspath), pattern) or (rel is not None and fnmatch(rel, pattern)):
            note = f" ({row['note']})" if row["note"] else ""
            return (
                f"cabildo: {_holder_name(conn, row)} holds `{row['resource']}`{note}, "
                f"expiring in {round(row['expires_at'] - store._now())}s — this edit "
                f"would touch {rel or path}. Coordinate via `send` before editing, "
                "or wait for the lease to expire."
            )
    return None


def _deny_destructive_git(
    conn: sqlite3.Connection, me: sqlite3.Row, command: str
) -> str | None:
    """Deny a working-tree-destroying git op while other agents are live on the
    project. Unlike push/file locks this is not tied to a claim: the op wipes
    the whole shared tree, so the trigger is simply 'someone else is here'."""
    chain = _chain_cwd(command, me["cwd"])
    label, where = None, chain
    for argv in _argvs(command):
        label = _destructive(argv)
        if label:
            where = _argv_cwd(argv, chain)
            break
    if label is None:
        return None
    # Only agents in the SAME CHECKOUT can be hurt by this. `project` collapses
    # linked worktrees onto the main repo (that is what makes one channel per
    # codebase work), so filtering on it denied a reset inside an isolated land
    # lane whenever anyone was anywhere on the repo — while telling the agent to
    # go and create the very worktree it was already standing in. The sibling
    # checks above have always filtered by tree_root; this one did not.
    mine = tree_root(where)
    peers = [
        p for p in store.active_sessions(conn, project=me["project"],
                                         exclude=me["session_id"])
        if p["cwd"] and tree_root(p["cwd"]) == mine
    ]
    if not peers:
        return None
    who = ", ".join(store.display_name(p) for p in peers[:5])
    return (
        f"cabildo: `{label}` in a shared working tree with {len(peers)} other "
        f"active agent(s) ({who}) can wipe their uncommitted work. Coordinate "
        "via `send`, or isolate in your own git worktree (`git worktree add`). "
        "Re-run once no one else is working the tree."
    )


def deny_reason(
    conn: sqlite3.Connection,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    """Reason to deny this tool call, or None to fall through. Never raises —
    a guard that crashes must not take the session down with it."""
    me = store.get_session(conn, session_id)
    if me is None or not me["project"]:
        return None
    if tool_name == "Bash" and me["project_name"]:
        command = tool_input.get("command") or ""
        return (
            _deny_push(conn, me, command)
            or _deny_blanket_stage(conn, me, command)
            or _deny_branch_switch(conn, me, command)
            or _deny_destructive_git(conn, me, command)
        )
    key = _FILE_TOOLS.get(tool_name)
    if key:
        path = tool_input.get(key)
        if path:
            return _deny_file(conn, me, str(path))
    return None


def _file_resource(me: sqlite3.Row, path: str) -> str:
    """The claim resource for a path: repo-relative when inside the project
    (portable, matches how humans claim `file:src/x.py`), else absolute."""
    project = me["project"]
    p = str(PurePosixPath(path))
    if project and p.startswith(project.rstrip("/") + "/"):
        return "file:" + p[len(project.rstrip("/")) + 1:]
    return "file:" + p


def autoclaim_on_edit(
    conn: sqlite3.Connection,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    """Take/refresh a short lease on a file this agent is *allowed* to edit, so
    the next agent to touch it hits _deny_file. Call this only after
    deny_reason returned None. Best-effort; returns the resource it claimed.

    This is what gives the guard teeth without agent discipline: claims were
    almost never made by hand, so nothing was ever enforced. Editing a file now
    *is* claiming it."""
    key = _FILE_TOOLS.get(tool_name)
    if not key:
        return None
    path = tool_input.get(key)
    if not path:
        return None
    me = store.get_session(conn, session_id)
    if me is None or not me["project"]:
        return None
    resource = _file_resource(me, str(path))
    store.acquire_claim(
        conn, resource=resource, holder_session=session_id,
        holder_handle=me["handle"], project=me["project"],
        ttl_seconds=AUTOCLAIM_TTL, note="auto: editing",
    )
    return resource
