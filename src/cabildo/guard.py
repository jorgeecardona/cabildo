"""Decision logic for the PreToolUse guard hook: stop the git collisions that
concurrent agents cause, before they happen rather than after.

Five conventions are enforced, all scoped to the session's own project:

  * ``git push`` — denied while ``push:<project_name>`` is held by someone else,
    and *also* denied while any other agent is simply live on the project, with
    the reason pointing at ``request push``. A lease you hold yourself is the
    deliberate escape hatch. (A push the remote then rejects non-fast-forward is
    not a collision to pre-block — ``push_failure_recipe`` coaches the recovery.)
  * force/delete push — ``git push --force``/``-f``/``--force-with-lease``/
    ``--delete``/``+<refspec>`` — denied by default whether or not a peer is live,
    because it rewrites or drops a remote ref and destroys committed work that
    lives only on the remote, which no fetch+rebase can recover. A repo opts out
    with ``[destructive].mode = "off"`` — a force-push is the remote-side member
    of the destructive family.
  * destructive git — ``git reset --hard``, a discarding ``checkout``/
    ``restore``, ``clean -f``, a tree-wide ``stash`` — is denied by default
    whether or not another agent is active, because it wipes uncommitted work an
    autonomous agent cannot bring back, dangerous even to a lone tree. A repo
    opts out with ``[destructive].mode = "off"``.
  * mutating git in a shared checkout — any op that writes the index, working
    tree, or HEAD (``add``, ``commit``, ``checkout``/``switch``, ``reset``,
    ``restore``, ``merge``, ``rebase``, …) — is denied while another agent stands
    in the *same* working tree, because that one checkout has one index, one
    tree, and one HEAD for them to collide on. The fix is not to referee each
    collision but to stop sharing the checkout: a worktree has its own, so the
    deny points at ``git worktree add``.
  * committing on a protected trunk — ``git commit`` while ``main``/``master``
    (or ``[trunk].names``) is checked out — is denied only where a repo opts in
    with ``[trunk].mode = "guard"``, because the daemon can only fast-forward a
    local trunk and a commit made straight on it diverges it from origin,
    drifting further behind every push to origin until ff can no longer reconcile
    it. It is off by default: a plain commit on ``main`` is the ordinary
    trunk-based flow, and the guard earns its cost only under the daemon-advances-
    origin/main-from-lanes model. Where on, branch and push from there instead.

Deny only ever fires against a live peer, a claim held by *someone else*, or a
discard that is irrecoverable on its own; everything else falls through to the
client's normal permission flow. Each reason names the holder/peers and the way
through, so the denied agent can go negotiate on the bus or move to a worktree
instead of retrying blind.
"""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
import subprocess
from typing import Any

from . import config, store
from .context import tree_root

# Pathspecs that name "the whole tree" rather than a specific file, so a discard
# that carries one is not path-scoped.
_WHOLE_TREE_PATHSPECS = {".", ":/", "*"}
# stash subcommands that inspect or re-apply, never discarding the working tree.
_STASH_SAFE = {"list", "show", "pop", "apply", "branch"}
# git's own options that swallow the next token, so the subcommand is found past
# `git -C /repo add`.
_GIT_OPT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# git subcommands that write the index, the working tree, or HEAD — the single
# copy of each that a shared checkout has. Read-only inspection (status/log/diff/
# show/branch-list/stash-list) is deliberately absent and never denied.
_CHECKOUT_MUTATORS = {
    "add", "commit", "checkout", "switch", "restore", "reset", "clean", "stash",
    "merge", "rebase", "cherry-pick", "revert", "am", "apply", "mv", "rm", "branch",
}  # fmt: skip


def _argvs(command: str) -> list[list[str]]:
    """A shell string split into one argv per command.

    Tokenising is what keeps `git commit -m "add -A support"` out of the
    subcommand checks: the message is a single token, and a token is not a flag.
    Unparseable input yields nothing — this guard catches the ordinary case, it
    does not try to out-parse bash.
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

    Tokenised for the same reason as the rest, plus one this file learned the
    hard way: `git stash push -m wip` contains "push" and is its opposite. A
    pattern that reads the string mislabels it; reading the subcommand cannot.
    """
    return any(_git_subcommand(argv)[0] == "push" for argv in _argvs(command))


def _push_target(command: str) -> str | None:
    """Best-effort: the remote branch a ``git push`` targets, for the recovery
    recipe. ``git push origin main`` -> main; ``git push origin HEAD:release`` ->
    release; a bare ``git push`` or anything unparsed -> None (the recipe then
    uses a ``<branch>`` placeholder the agent fills in).
    """
    for argv in _argvs(command):
        sub, rest = _git_subcommand(argv)
        if sub != "push":
            continue
        positionals = [a for a in rest if not a.startswith("-")]
        if len(positionals) >= 2:
            dst = positionals[1].split(":")[-1].lstrip("+")
            if dst and dst != "HEAD":
                return dst
    return None


# The remote rejected a push because our tip is behind — the only push failure
# the fetch+rebase recipe answers. Other failures (auth, no such remote) are not
# ours to coach. Matched case-insensitively against the command's output.
_PUSH_REJECTED = re.compile(
    r"non-fast-forward|fetch first|\[rejected\]|stale info|tip of your current branch is behind",
    re.IGNORECASE,
)


def push_failure_recipe(command: str, output: str | None) -> str | None:
    """The non-destructive recovery for a ``git push`` the remote rejected because
    it moved (non-fast-forward), or None if this was not a rejected push.

    This is the whole point of *not* pre-blocking an ordinary push: git already
    rejects the loser of a race, and the correct recovery is fetch + rebase +
    re-push. The danger was never the rejection — it is an agent reaching for
    ``reset --hard``/``push --force``/a merge ``pull`` for lack of the right
    instruction, and those discard the commits it has not fetched yet. So hand it
    the instruction at the moment it is rejected. Pure text match on the output,
    identical for Claude Code's ``tool_response`` and Codex's.
    """
    if not command or not output or not _is_push(command) or not _PUSH_REJECTED.search(output):
        return None
    branch = _push_target(command) or "<branch>"
    return (
        "cabildo: that push was rejected — the remote moved since you last fetched "
        "(someone pushed in between). Recover without discarding their commits:\n"
        "    git fetch origin\n"
        f"    git rebase origin/{branch}\n"
        f"    git push origin HEAD:{branch}\n"
        "If the rebase conflicts, resolve and `git rebase --continue`, or `git rebase "
        "--abort` to back out and coordinate on the bus. Do NOT `git reset --hard`, "
        "`git push --force`, or a merge `git pull` here — each discards the commits "
        "you have not fetched yet."
    )


def _paths_after_ddash(args: list[str]) -> list[str]:
    """Everything after a lone ``--`` end-of-options marker; [] if there is none."""
    return args[args.index("--") + 1 :] if "--" in args else []


def _destroys_tree(argv: list[str]) -> str | None:
    """Label for a git command that discards working-tree state, else None.

    Classified on *tokens* (via _git_subcommand), not the raw string, for two
    things the raw-regex form got wrong (issue #19):

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
        # A plain branch switch is the shared-checkout rule's job, not this one.
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


def _force_pushes(argv: list[str]) -> str | None:
    """Label for a ``git push`` that rewrites or deletes a remote ref, else None.

    This is the one git op that destroys another agent's already-*committed*
    work: a plain push is rejected non-fast-forward and recovered with
    fetch+rebase, but ``--force`` overwrites the remote tip and ``--delete`` drops
    the ref outright, discarding commits that live only on the remote. Classified
    on tokens, like its siblings.

      * ``--force`` / ``-f`` (bundled short flags too, ``-fv``).
      * ``--force-with-lease`` (``=...`` form included): safer — it refuses a
        stale overwrite — but it still discards the peer commits you *did* fetch,
        so it is denied and opted out of deliberately, not reached for by habit.
      * ``--delete`` / ``-d``.
      * a force refspec, ``+<src>[:<dst>]`` (``git push origin +main``).
    """
    sub, args = _git_subcommand(argv)
    if sub != "push":
        return None
    for a in args:
        if a in ("--force", "--delete"):
            return f"git push {a}"
        if a == "--force-with-lease" or a.startswith("--force-with-lease="):
            return "git push --force-with-lease"
        if a.startswith("-") and not a.startswith("--") and ("f" in a[1:] or a == "-d"):
            return "git push -f" if "f" in a[1:] else "git push --delete"
    return next(
        (
            "git push +<refspec>"
            for a in args
            if not a.startswith("-") and a.startswith("+") and len(a) > 1
        ),
        None,
    )


def _mutates_shared_checkout(argv: list[str]) -> str | None:
    """Label for a git op that writes the index, working tree, or HEAD — the
    state a checkout has exactly one of — else None. Read-only inspection is
    never labelled, and a handful of read-only forms of mutating subcommands are
    carved out (``commit --dry-run``, bare ``checkout``/``switch``, ``stash
    list``/``show``, ``apply --check``).
    """
    sub, args = _git_subcommand(argv)
    if sub not in _CHECKOUT_MUTATORS:
        return None
    if sub == "commit" and any(a in ("--dry-run", "-h", "--help") for a in args):
        return None
    if sub in ("checkout", "switch"):
        positional = [a for a in args if not a.startswith("-")]
        creating = any(a in ("-b", "-B", "-c", "-C") for a in args)
        if not positional and not creating and "--" not in args:
            return None  # bare checkout/switch prints the current branch
    if sub == "stash":
        action = next((a for a in args if not a.startswith("-")), None)
        if action in ("list", "show"):
            return None
    if sub == "apply" and "--check" in args:
        return None
    if sub == "branch":
        # Listing or creating a label touches neither the tree nor HEAD; only
        # force-moving a label somebody else has checked out does (the same
        # accident as a switch, wearing different clothes — 2026-08-03).
        if not any(a in ("-f", "--force", "-M", "-m") for a in args):
            return None
        return "git branch -f"
    return f"git {sub}"


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


def _sharers(conn: sqlite3.Connection, me: sqlite3.Row) -> list[sqlite3.Row]:
    """The live peers standing in the *same* working tree as ``me``. cwd is what
    says which checkout; project can't (it collapses worktrees). A row without a
    cwd tells us nothing, so it is not treated as a sharer.
    """
    if not me["cwd"]:
        return []
    mine = tree_root(me["cwd"])
    return [
        p
        for p in store.active_sessions(conn, project=me["project"], exclude=me["session_id"])
        if p["cwd"] and tree_root(p["cwd"]) == mine
    ]


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
        f'    request(action="push", params={{"cwd": "{me["cwd"] or me["project"]}", '
        '"target": "<branch you are pushing on, e.g. main>"})\n'
        "It pushes commits and never decides what to commit, so commit first and "
        "leave no tracked changes behind. It DMs you the result. If you really "
        f"must push by hand, take `{resource}` with `claim` first — that denies "
        "the others and makes the rebase yours to own."
    )


def _deny_force_push(me: sqlite3.Row, command: str) -> str | None:
    """Deny a force/delete push. Denied by default whether or not a peer is live:
    it destroys committed work on the remote, which no fetch+rebase can bring
    back, so it is dangerous even to a lone agent. A repo opts out with
    ``[destructive].mode = "off"`` — the same switch that governs working-tree
    discards, because a force-push is the remote-side member of that family.
    """
    label = next((lbl for argv in _argvs(command) if (lbl := _force_pushes(argv))), None)
    if label is None:
        return None
    if config.destructive_mode(me["cwd"], me["project_name"]) == "off":
        return None  # this project opted out (.cabildo/config.toml)
    return (
        f"cabildo: `{label}` rewrites or deletes a ref on the remote, discarding commits "
        f"that live only there — including a peer's already-pushed work, which no "
        f"fetch+rebase can bring back. Recover a rejected push the non-destructive way "
        f"instead:\n"
        f"    git fetch origin && git rebase origin/<branch> && git push origin HEAD:<branch>\n"
        f"If you genuinely must rewrite the remote (e.g. your own feature branch), opt out "
        f'per-repo with `[destructive].mode = "off"` in .cabildo/config.toml.'
    )


def _deny_destructive_git(me: sqlite3.Row, command: str) -> str | None:
    """Deny a working-tree-destroying git op (``reset --hard``, ``clean -f``, a
    discarding ``checkout``/``restore``, a tree-wide ``stash``). Denied whether or
    not a peer is here — it discards uncommitted work an autonomous agent cannot
    bring back, dangerous even to a lone tree. A repo opts out with
    ``[destructive].mode = "off"``. (A peer sharing the tree is caught by the
    shared-checkout rule for every *other* mutating op too; this one fires
    regardless, because the discard is irrecoverable on its own.)
    """
    label = next((lbl for argv in _argvs(command) if (lbl := _destroys_tree(argv))), None)
    if label is None:
        return None
    if config.destructive_mode(me["cwd"], me["project_name"]) == "off":
        return None  # this project opted out (.cabildo/config.toml)
    return (
        f"cabildo: `{label}` clears uncommitted work across the whole tree, and an "
        "autonomous agent should not throw away changes it cannot bring back. "
        "Commit or stash-list what matters first. If you really mean it, opt out "
        'per-repo with `[destructive].mode = "off"` in .cabildo/config.toml.'
    )


def _deny_shared_checkout(conn: sqlite3.Connection, me: sqlite3.Row, command: str) -> str | None:
    """Deny a git op that writes the index, working tree, or HEAD while another
    agent shares this exact checkout.

    A checkout has one index, one working tree, one HEAD. Two agents standing in
    it collide on all three: one stages or commits the other's half-written
    files, or moves the branch under them so the next commit lands on a branch
    nobody chose — which happened three times in this repo's own fleet on
    2026-08-03, once pushing three commits of unrelated work onto someone else's
    feature branch before anyone noticed. The fix is not to referee each
    collision but to stop sharing the checkout: a worktree has its own index,
    tree, and HEAD. So any mutating git here, while a peer shares the tree, is
    denied and pointed at ``git worktree add``.
    """
    label = next((lbl for argv in _argvs(command) if (lbl := _mutates_shared_checkout(argv))), None)
    if label is None:
        return None
    sharers = _sharers(conn, me)
    if not sharers:
        return None
    who = ", ".join(store.display_name(p) for p in sharers[:5])
    return (
        f"cabildo: `{label}` writes the index, working tree, or HEAD that you share with "
        f"{len(sharers)} other agent(s) ({who}) in this one checkout — it can stage their "
        f"half-written files, wipe their uncommitted work, or move the branch under them. "
        f"Work in your own tree instead:\n"
        f"    git worktree add ../wt-<name> -b <name>\n"
        f"then run it there (Claude Code: the EnterWorktree tool does this in one call)."
    )


def _commits(argv: list[str]) -> bool:
    """True if this git argv *writes* a commit (``git commit`` that is not a
    ``--dry-run`` or a help invocation). ``--amend`` still rewrites the branch
    tip, so it counts. Classified on tokens, like its siblings: ``git commit``
    inside a quoted message (``git log --grep "git commit"``) is never a git
    commit argv, so it never matches.
    """
    sub, args = _git_subcommand(argv)
    if sub != "commit":
        return False
    return not any(a in ("--dry-run", "-h", "--help") for a in args)


def _current_branch(root: str) -> str | None:
    """The branch checked out at ``root``, or None on a detached HEAD, a
    non-repo, or any git error. Best-effort and never raises — a guard that
    cannot read the branch must fall through, not block every commit.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", root, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = proc.stdout.strip()
    return name or None


def _dash_c_dir(argv: list[str]) -> str | None:
    """The directory a ``git -C <path>`` argv operates in, or None. Git applies
    multiple ``-C`` cumulatively; the common case is one and the last wins.
    """
    path: str | None = None
    i = 1
    while i < len(argv) - 1:
        if argv[i] == "-C":
            path = argv[i + 1]
            i += 2
            continue
        i += 1
    return path


def _lone_cd_target(command: str) -> str | None:
    """If the command chain contains exactly one ``cd <path>``, that path; else
    None. A single ``cd`` sets the cwd for the git that follows; two are
    ambiguous, so we fall back to the session cwd rather than guess.
    """
    cds = [argv[1] for argv in _argvs(command) if len(argv) >= 2 and argv[0] == "cd"]
    return cds[0] if len(cds) == 1 else None


def _commit_dir(argv: list[str], command: str, default: str) -> str:
    """Where a ``git commit`` actually runs: its own ``-C <path>``, else a lone
    ``cd <path>`` in the chain, else the session's cwd. A relative path resolves
    against the session cwd. This is the fix for the guard judging a commit by
    where the SESSION was launched (the main checkout, on ``main``) instead of
    where the command targets (a lane worktree) — which blocked legitimate
    commits on a lane from a session rooted in main.
    """
    target = _dash_c_dir(argv) or _lone_cd_target(command) or default
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(default, target))
    return target


def _deny_commit_on_trunk(me: sqlite3.Row, command: str) -> str | None:
    """Deny an agent committing directly on a protected trunk branch
    (``main``/``master``, or ``[trunk].names``).

    The daemon advances ``origin/main`` from lanes and deliberately never
    force-updates a checked-out branch, so it can only *fast-forward* a local
    trunk — which is impossible the moment that trunk carries its own commits.
    A commit made straight on local ``main`` therefore diverges it from origin
    and drifts one commit further per push to origin, until ff can no longer
    reconcile it and the only way back is a history rewrite. Stopping the commit
    is the one cheap point in that sequence; every later fix (reset, rebase,
    force-push) destroys more than the divergence did.

    So the guard is off by default — a plain commit on ``main`` is the ordinary
    trunk-based flow — and a repo that runs the daemon-advances-origin/main model
    opts in with ``[trunk].mode = "guard"``. Where on, the intended workflow is
    the escape hatch: branch, commit on the lane, push it to origin from there.
    """
    if not me["cwd"] or config.trunk_mode(me["cwd"], me["project_name"]) != "guard":
        return None
    trunk = config.trunk_names(me["cwd"], me["project_name"])
    # Judge each commit by the branch of the directory it ACTUALLY targets (its
    # -C/cd, else the session cwd) — not by where the session was launched. A
    # commit on a lane worktree is fine even from a session rooted in main.
    for argv in _argvs(command):
        if not _commits(argv):
            continue
        root = tree_root(_commit_dir(argv, command, me["cwd"]))
        if not root:
            continue
        branch = _current_branch(root)
        if branch is None or branch not in trunk:
            continue
        return (
            f"cabildo: committing directly on `{branch}` in {root} diverges your "
            f"local trunk from origin. The daemon advances origin/{branch} from "
            f"lanes and can only fast-forward a local trunk, never rewrite one that "
            f"carries its own commits — so this commit strands {branch} and it "
            f"drifts further behind every push to origin until ff can no longer "
            f"reconcile it. Work on a lane and push it to origin, leaving {branch} a "
            f"ref the daemon keeps current:\n"
            f"    git switch -c <lane>   # or an isolated tree: git worktree add ../wt-<lane> -b <lane>\n"
            f"    …commit on <lane>, then queue the push to origin:\n"
            f'    request(action="push", params={{"cwd": "{root}", "target": "{branch}"}})\n'
            f'Opt out per-repo with `[trunk].mode = "off"` in .cabildo/config.toml.'
        )
    return None


def deny_reason(
    conn: sqlite3.Connection,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    """Reason to deny this tool call, or None to fall through. Never raises —
    a guard that crashes must not take the session down with it. Only Bash is
    gated now: file writes (Edit/Write/apply_patch) are coordinated by isolating
    agents in their own worktrees, not by a per-file lease.
    """
    me = store.get_session(conn, session_id)
    if me is None or not me["project"]:
        return None
    if tool_name == "Bash" and me["project_name"]:
        command = tool_input.get("command") or ""
        return (
            _deny_force_push(me, command)
            or _deny_push(conn, me, command)
            or _deny_destructive_git(me, command)
            or _deny_commit_on_trunk(me, command)
            or _deny_shared_checkout(conn, me, command)
        )
    return None
