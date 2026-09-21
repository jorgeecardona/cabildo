"""Decision logic for the PreToolUse guard hook: turn advisory claims into
enforced ones at the tool-call boundary.

Four conventions are enforced, all scoped to the session's own project:

  * ``file:<path>`` — a write to a matching path is denied, whether it arrives
    as Edit/MultiEdit/Write/NotebookEdit, a Codex ``apply_patch``, or a shell
    write that would otherwise slip past the lease (``echo > f``, ``tee f``,
    ``sed -i f``, ``dd of=f``, a literal ``open(...,'w')`` under ``python -c``).
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
  * destructive git — ``git reset --hard``, a discarding ``checkout``/
    ``restore``, ``clean -f``, ``stash`` — is denied by default whether or not
    another agent is active, because it wipes the tree's uncommitted work, not
    just one claimed file, and an autonomous agent should not throw away changes
    it cannot bring back. A repo opts out with ``[destructive].mode = "off"``.

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
import re
import shlex
import sqlite3
from pathlib import PurePosixPath
from typing import Any

from . import config, store
from .context import tree_root

# Pathspecs that name "the whole tree" rather than a specific file, so a discard
# that carries one is not path-scoped. Mirrors the whole-tree markers in
# _STAGE_ALL below.
_WHOLE_TREE_PATHSPECS = {".", ":/", "*"}
# stash subcommands that inspect or re-apply, never discarding the working tree.
_STASH_SAFE = {"list", "show", "pop", "apply", "branch"}

# Staging the whole tree rather than named paths. Matched on *tokens*, not on
# the raw string: `git commit -m "add -A support"` must not read as `git add -A`,
# and a message is one token while a flag is another.
_STAGE_ALL = {"-A", "--all", "-u", "--update", "--no-ignore-removal", ".", ":/"}
# git's own options that swallow the next token, so the subcommand is found past
# `git -C /repo add`.
_GIT_OPT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# Shell redirects that write a *file* (the token after them is the path), as
# opposed to descriptor duplication (`>&`, `2>&1`) or a pipe. `>|` is the
# noclobber-override form; `&>`/`&>>` send stdout+stderr to the file.
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
# Shell metacharacters shlex(punctuation_chars=True) returns as their own tokens.
# A token made only of these is an operator, never a path.
_PUNCT = set("<>|&;()")
_INTERPRETERS = {"python", "python3", "perl", "ruby", "node"}
# Best-effort: a literal open('path', 'w'|'a'|'x'…) inside `python -c "…"`. Only
# the literal-string form is caught; a path built from a variable is not.
_OPEN_WRITE = re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][^'"]*[wax]""")

_FILE_TOOLS = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

# How long an auto-claim taken on edit lasts. Short, and refreshed on every
# re-edit, so an agent that moves on releases the file within minutes without
# needing to remember; SessionEnd drops it immediately.
AUTOCLAIM_TTL = 600.0


def _argvs(command: str) -> list[list[str]]:
    """A shell string split into one argv per command.

    Tokenising is what keeps `git commit -m "add -A support"` out of the
    blanket-stage check: the message is a single token, and a token is not a
    flag. Unparseable input yields nothing — this guard catches the ordinary
    case, it does not try to out-parse bash.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:  # unbalanced quotes; not ours to fix
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
        return tok, argv[i + 1 :]
    return None, []


def _is_push(command: str) -> bool:
    """True if `command` runs `git push` — the subcommand, not the word.

    Tokenised for the same reason as staging, plus one this file learned the
    hard way: `git stash push -m wip` contains "push" and is its opposite. A
    pattern that reads the string mislabels it; reading the subcommand cannot.
    """
    return any(_git_subcommand(argv)[0] == "push" for argv in _argvs(command))


def _stages_whole_tree(argv: list[str]) -> str | None:
    """Label for a command that stages or commits the *whole* tree instead of
    named paths, else None. `git add src/x.py` is fine; `git add -A` is not.
    """
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


def _paths_after_ddash(args: list[str]) -> list[str]:
    """Everything after a lone ``--`` end-of-options marker; [] if there is none."""
    return args[args.index("--") + 1 :] if "--" in args else []


def _destroys_tree(argv: list[str]) -> str | None:
    """Label for a git command that discards working-tree state, else None.

    Classified on *tokens* (via _git_subcommand), not the raw string, for the
    same reason as the sibling guards, and for two the raw-regex form got wrong
    (issue #19):

      * a destructive word inside a quoted argument (``grep "git stash"``) is
        never a git argv, so it never matches — no false positive on a mention.
      * a discard limited to named paths (``git restore <path>``,
        ``git stash push -- <paths>``) is told apart from its tree-wide form; it
        cannot reach a peer's other files, so it is allowed.

    Deny is the safe default: an ambiguous or whole-tree discard is denied, and
    only a discard clearly scoped to named paths is let through.
    """
    sub, args = _git_subcommand(argv)
    if sub is None:
        return None
    if sub == "reset":
        return "git reset --hard" if "--hard" in args else None
    if sub == "checkout":
        # A discard: `checkout .` or a path-restricted `checkout ... -- <path>`.
        # A plain branch switch (no `--`, a ref positional) is _switches_branch's
        # job and returns None here.
        if "--" in args:
            return "git checkout (discard)"
        if any(a in _WHOLE_TREE_PATHSPECS for a in args if not a.startswith("-")):
            return "git checkout (discard)"
        return None
    if sub == "restore":
        # --staged only unstages; it leaves the working tree untouched.
        if "--staged" in args and "--worktree" not in args:
            return None
        specs = _paths_after_ddash(args) or [a for a in args if not a.startswith("-")]
        concrete = [s for s in specs if s not in _WHOLE_TREE_PATHSPECS]
        return None if concrete else "git restore (discard)"
    if sub == "clean":
        forceful = any(
            a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a[1:])
            for a in args
        )
        return "git clean -f" if forceful else None
    if sub == "stash":
        action = next((a for a in args if not a.startswith("-")), None)
        if action in _STASH_SAFE:
            return None
        if action == "push" and any(
            s not in _WHOLE_TREE_PATHSPECS for s in _paths_after_ddash(args)
        ):
            return None  # `git stash push -- <paths>`: path-scoped, safe
        return "git stash"
    return None


def _held_by_other(conn: sqlite3.Connection, resource: str, session_id: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM claims WHERE resource=? AND expires_at>? AND holder_session!=?",
        (resource, store._now(), session_id),
    ).fetchone()
    return row


def _holder_name(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    return store.display_name(store.get_session(conn, row["holder_session"]), row["holder_session"])


def _held_by_me(conn: sqlite3.Connection, resource: str, session_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM claims WHERE resource=? AND expires_at>? AND holder_session=?",
            (resource, store._now(), session_id),
        ).fetchone()
        is not None
    )


def _deny_push(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
    if not _is_push(command):
        return None
    if config.push_mode(me["cwd"], me["project_name"]) == "manual":
        return None  # this project opted out of the push guard (.cabildo/config.toml)
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
        return None  # you took the lock: peers are the ones denied
    peers = store.active_sessions(conn, project=me["project"], exclude=me["session_id"])
    if not peers:
        return None  # alone: push is nobody's business but yours
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


def _deny_blanket_stage(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
    """Deny `git add -A` / `git commit -a` while another agent shares this exact
    working tree.

    Staging is not dangerous; staging *indiscriminately* is. In a shared
    checkout it sweeps whatever the other agent has half-written into your
    commit, and the fix for that — reset, revert, force-push — destroys more
    than the mistake did. Separate worktrees have separate indexes, so this
    never fires on agents who isolated themselves properly.
    """
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
    mine = tree_root(me["cwd"])
    sharers = [
        p
        for p in store.active_sessions(conn, project=me["project"], exclude=me["session_id"])
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
            return None  # `git checkout <tree-ish> -- <path>`: a restore
        if any(a in ("-b", "-B", "-c", "-C") for a in args):
            return f"git {sub} -b"
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            return None  # bare `git checkout`: prints, changes nothing
        if sub == "checkout" and any(os.sep in a or os.path.exists(a) for a in positional):
            return None  # looks like a path, not a ref
        return f"git {sub}"
    if sub == "branch" and any(a in ("-f", "--force", "-M", "-m") for a in args):
        return "git branch -f"
    return None


def _deny_branch_switch(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
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
    mine = tree_root(me["cwd"])
    sharers = [
        p
        for p in store.active_sessions(conn, project=me["project"], exclude=me["session_id"])
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
        rel = str(abspath)[len(project.rstrip("/")) + 1 :]
    candidates = [str(abspath)] + ([rel] if rel is not None else [])
    for row in conn.execute(
        "SELECT * FROM claims WHERE resource LIKE 'file:%' AND expires_at>? "
        "AND holder_session!=? AND project=?",
        (store._now(), me["session_id"], project),
    ):
        pattern = row["resource"][len("file:") :]
        if store.file_pattern_matches(pattern, candidates):
            note = f" ({row['note']})" if row["note"] else ""
            return (
                f"cabildo: {_holder_name(conn, row)} holds `{row['resource']}`{note}, "
                f"expiring in {round(row['expires_at'] - store._now())}s — this edit "
                f"would touch {rel or path}. Coordinate via `send` before editing, "
                "or wait for the lease to expire."
            )
    return None


def _deny_destructive_git(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
    """Deny a working-tree-destroying git op. Denied by default whether or not
    another agent shares the tree -- the op discards uncommitted work an
    autonomous agent cannot bring back, which is dangerous even to a lone agent's
    own tree. A repo opts out with ``[destructive].mode = "off"``. When peers do
    share the tree the reason names them, because then it wipes *their* work too.
    """
    label = next((lbl for argv in _argvs(command) if (lbl := _destroys_tree(argv))), None)
    if label is None:
        return None
    if config.destructive_mode(me["cwd"], me["project_name"]) == "off":
        return None  # this project opted out (.cabildo/config.toml)
    optout = 'opt out per-repo with `[destructive].mode = "off"` in .cabildo/config.toml'
    peers = store.active_sessions(conn, project=me["project"], exclude=me["session_id"])
    if peers:
        who = ", ".join(store.display_name(p) for p in peers[:5])
        return (
            f"cabildo: `{label}` in a shared working tree with {len(peers)} other "
            f"active agent(s) ({who}) can wipe their uncommitted work. Coordinate "
            "via `send`, or isolate in your own git worktree (`git worktree add`). "
            f"Re-run once no one else is working the tree, or {optout}."
        )
    return (
        f"cabildo: `{label}` clears uncommitted work across the whole tree, and an "
        "autonomous agent should not throw away changes it cannot bring back. "
        f"Commit or stash-list what matters first. If you really mean it, {optout}."
    )


def _shell_tokens(command: str) -> list[str]:
    """A shell command tokenised with redirects/pipes as their own tokens and
    quotes respected, so a `>` inside ``"print(1 > 0)"`` is not read as a
    redirect. Unparseable input yields nothing — same contract as _argvs.
    """
    lx = shlex.shlex(command, posix=True, punctuation_chars=True)
    lx.whitespace_split = True
    try:
        return list(lx)
    except ValueError:  # unbalanced quotes; not ours to fix
        return []


def _is_operator(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT for c in tok)


def _under_project(path: str, project: str | None) -> bool:
    """True if an absolute path lies inside the project tree."""
    if not project:
        return False
    return str(PurePosixPath(path)).startswith(project.rstrip("/") + "/")


def _split_pipeline(tokens: list[str]) -> list[list[str]]:
    """The word tokens grouped into one argv per simple command, splitting on
    every operator token (`|`, `;`, `&&`, a redirect, …). Redirect *targets*
    land in their own single-word group, which no writer-command check matches,
    so they are harmless here — redirects are handled separately by path.
    """
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if _is_operator(tok):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _sed_in_place(args: list[str]) -> bool:
    """True if a sed argv edits its files in place (`-i`, `-i.bak`, a bundled
    `-ni`, or `--in-place`).
    """
    for a in args:
        if a.startswith("--in-place"):
            return True
        if a.startswith("-") and not a.startswith("--") and "i" in a[1:]:
            return True
    return False


def _sed_files(args: list[str]) -> list[str]:
    """The files a `sed -i` argv rewrites. With an `-e`/`-f` script option every
    positional is a file; otherwise the first positional is the inline script and
    the rest are files. Best-effort, precision-first.
    """
    script_opt = ("-e", "-f", "--expression", "--file")
    has_script_opt = any(
        a in script_opt or a.startswith(("--expression=", "--file=")) for a in args
    )
    positionals: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if a in script_opt:
                skip = True  # its value is the next token
            continue
        positionals.append(a)
    return positionals if has_script_opt else positionals[1:]


def _interp_code(args: list[str]) -> str | None:
    """The code string an interpreter is asked to run inline (`-c`/`-e`)."""
    for i, a in enumerate(args):
        if a in ("-c", "-e") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("-c") and len(a) > 2:
            return a[2:]
    return None


def _writer_targets(argv: list[str]) -> list[str]:
    """Files a single non-redirect command evidently writes: `tee f`, `sed -i f`,
    `dd of=f`, or a literal open(...,'w') under `python -c`.
    """
    if not argv:
        return []
    cmd = os.path.basename(argv[0])
    args = argv[1:]
    if cmd == "tee":
        return [a for a in args if not a.startswith("-")]
    if cmd == "sed" and _sed_in_place(args):
        return _sed_files(args)
    if cmd == "dd":
        return [a[len("of=") :] for a in args if a.startswith("of=") and len(a) > 3]
    if cmd in _INTERPRETERS:
        code = _interp_code(args)
        return _OPEN_WRITE.findall(code) if code else []
    return []


def _shell_write_paths(command: str, cwd: str | None) -> list[str]:
    """Absolute, de-duplicated paths a Bash command evidently *writes*: `>`/`>>`
    redirects, `tee`, `sed -i`, `dd of=`, and a literal open(...,'w') in
    `python -c`. Best-effort and precision-first — it catches the ordinary shell
    write that walks straight past a file lease, it does not try to out-parse bash
    (a path built from a variable, an exotic writer, is not caught).
    """
    tokens = _shell_tokens(command)
    if not tokens:
        return []
    raw: list[str] = []
    for i, tok in enumerate(tokens[:-1]):
        if tok in _WRITE_REDIRECTS and not _is_operator(tokens[i + 1]):
            raw.append(tokens[i + 1])
    for argv in _split_pipeline(tokens):
        raw.extend(_writer_targets(argv))
    base = cwd or os.getcwd()
    paths = [os.path.normpath(os.path.join(base, p)) for p in raw if p]
    return list(dict.fromkeys(paths))


def _deny_shell_write(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
    """Deny a shell command that writes a file another agent holds a lease on.
    This closes the gap Edit/Write/apply_patch already covered: a `sed -i`,
    `echo > f` or `tee f` on a claimed file would otherwise walk straight past
    the lease.
    """
    for path in _shell_write_paths(command, me["cwd"]):
        reason = _deny_file(conn, me, path)
        if reason:
            return reason
    return None


def deny_reason(
    conn: sqlite3.Connection,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    """Reason to deny this tool call, or None to fall through. Never raises —
    a guard that crashes must not take the session down with it.
    """
    me = store.get_session(conn, session_id)
    if me is None or not me["project"]:
        return None
    if tool_name == "apply_patch":
        for path in _patch_paths(tool_input.get("command"), me["cwd"]):
            reason = _deny_file(conn, me, path)
            if reason:
                return reason
        return None
    if tool_name == "Bash" and me["project_name"]:
        command = tool_input.get("command") or ""
        return (
            _deny_push(conn, me, command)
            or _deny_blanket_stage(conn, me, command)
            or _deny_branch_switch(conn, me, command)
            or _deny_destructive_git(conn, me, command)
            or _deny_shell_write(conn, me, command)
        )
    key = _FILE_TOOLS.get(tool_name)
    if key:
        path = tool_input.get(key)
        if path:
            return _deny_file(conn, me, str(path))
    return None


def _patch_paths(command: Any, cwd: str | None) -> list[str]:
    """Paths touched by Codex's `apply_patch` envelope, resolved to absolute and
    de-duplicated. Covers additions, deletions, updates, and both ends of a move
    (the source `*** Update File:` plus its `*** Move to:` destination).
    """
    if not isinstance(command, str):
        return []
    prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
    paths = []
    for line in command.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                path = line[len(prefix) :].strip()
                if path:
                    paths.append(os.path.normpath(os.path.join(cwd or os.getcwd(), path)))
                break
    return list(dict.fromkeys(paths))


def _file_resource(me: sqlite3.Row, path: str) -> str:
    """The claim resource for a path: repo-relative when inside the project
    (portable, matches how humans claim `file:src/x.py`), else absolute.
    """
    project = me["project"]
    p = str(PurePosixPath(path))
    if project and p.startswith(project.rstrip("/") + "/"):
        return "file:" + p[len(project.rstrip("/")) + 1 :]
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
    *is* claiming it.
    """
    if tool_name in ("apply_patch", "Bash"):
        me = store.get_session(conn, session_id)
        if me is None:
            return None
        if tool_name == "apply_patch":
            written = _patch_paths(tool_input.get("command"), me["cwd"])
        else:
            # Only in-project writes are auto-claimed: a shell redirect to
            # /dev/null or /tmp/build.log is not a coordination target, and
            # auto-claiming it would both litter the claims table and falsely
            # block a peer writing the same throwaway path. An explicit `claim`
            # on such a path still enforces via _deny_file.
            written = [
                p
                for p in _shell_write_paths(tool_input.get("command") or "", me["cwd"])
                if _under_project(p, me["project"])
            ]
        resources = [
            autoclaim_on_edit(conn, session_id, "Edit", {"file_path": path}) for path in written
        ]
        return next((r for r in resources if r), None)
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
        conn,
        resource=resource,
        holder_session=session_id,
        holder_handle=me["handle"],
        project=me["project"],
        ttl_seconds=AUTOCLAIM_TTL,
        note="auto: editing",
    )
    return resource
