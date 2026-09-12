"""What the daemon is allowed to do, and how.

Every action is an explicit entry in ACTIONS. There is no generic "run this
command" action and there never should be: an agent that could name the command
would make the daemon a remote shell for anything that can write to the DB.
Agents choose an action and supply typed params; the code here decides what
actually runs.

Handlers take (params, ctx) and return a result dict. They must not raise for
ordinary failure — a failed land is data, not an exception. They may raise for
programmer error; the daemon catches and records that too.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

GIT_TIMEOUT = 120
# The gate is the referee: it can run a whole test suite, so it gets room.
GATE_TIMEOUT = 1800
# Repo-owned, not requester-supplied. The agent asking to land does not get to
# choose the thing that judges it.
GATE_PATH = ".cabildo/gate.sh"

# Worktrees live beside the repo, never inside it: a worktree under the repo
# shows up in its own status and gets swept into someone's `git add`, which is
# the exact failure this is meant to end.
WORKTREE_DIRNAME = ".agent-worktrees"

# What a spawned agent may do on the bus. It needs a voice to be useful and
# `request` to land its work; it does not need to spawn more agents.
BUS_TOOLS = [
    "mcp__cabildo__whoami",
    "mcp__cabildo__send",
    "mcp__cabildo__inbox",
    "mcp__cabildo__read",
    "mcp__cabildo__list_agents",
    "mcp__cabildo__set_handle",
    "mcp__cabildo__claim",
    "mcp__cabildo__release",
    "mcp__cabildo__claims",
    "mcp__cabildo__request",
    "mcp__cabildo__intents",
]


@dataclass
class Ctx:
    """Daemon-level settings. Deliberately not reachable from intent params —
    an agent must not be able to talk the daemon into pushing, spawning, or
    raising its own budget.
    """

    worker: str
    allow_push: bool = False
    allow_spawn: bool = False
    max_agents: int = 3
    # How deep spawn chains may go: human→agent is depth 1, that agent's helper
    # is depth 2. The limit exists because delegation compounds — every level
    # multiplies the burn and divides the accountability.
    max_depth: int = 2
    # Weighted-token ceiling for the trailing window; 0 disables the throttle.
    budget: float = 0.0
    budget_days: float = 7.0
    # Seconds a queued intent may wait before the daemon refuses it instead of
    # running it. A request is a snapshot of a moment: the branch it wanted to
    # land on has moved, the agent that asked is gone, and nobody is watching the
    # result. Draining a week-old queue on startup is how a daemon does damage
    # while technically doing what it was told. 0 disables the ceiling.
    max_intent_age: float = 2 * 3600.0
    log: Callable[[str], None] = lambda _m: None


class Reject(Exception):
    """The request is not valid or not safe. Terminal, not retryable."""


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #
def run(args: list[str], cwd: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    """Run argv (never a shell string) and return (rc, combined output)."""
    try:
        p = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Keep the daemon's own git out of the agents' interactive auth.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {shlex.join(args)}"
    except FileNotFoundError as e:
        return 127, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def git(args: list[str], cwd: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    return run(["git", *args], cwd, timeout)


# Files Claude Code rewrites by itself — accepting a permission prompt edits
# settings.json mid-session. When they are the *only* thing standing between an
# agent and a land, the agent didn't do it and won't think to look there.
_CLAUDE_OWN = (".claude/settings.json", ".claude/settings.local.json")


def _porcelain_path(line: str) -> str:
    """The path out of one `git status --porcelain` line.

    Not a fixed slice: the XY status field is two columns, but the leading space
    of ' M file' is gone by the time output reaches us (git()'s .strip()), so a
    line is 'M file' or 'MM file' depending on the state. Split off the first
    field instead, and take the destination of a rename.
    """
    body = line.strip()
    body = body.split(maxsplit=1)[1] if " " in body else body
    if " -> " in body:  # 'R  old -> new'
        body = body.split(" -> ", 1)[1]
    return body.strip().strip('"')


def _claude_settings_hint(dirty_lines: list[str]) -> str:
    paths = [_porcelain_path(ln) for ln in dirty_lines]
    if paths and all(p in _CLAUDE_OWN for p in paths):
        return (
            "\n\nNote: that is Claude Code's own settings file, which it "
            "rewrites when you accept a permission — probably not your work. "
            "Commit it or `git checkout --` it, then re-request."
        )
    return ""


# --------------------------------------------------------------------------- #
# land
# --------------------------------------------------------------------------- #
def land(params: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Serialised fetch → rebase → gate → push for one worktree.

    Only commits are landed: the daemon never stages or commits for you. What is
    in your tree but uncommitted is yours to resolve, and a robot guessing at it
    is how another agent's work gets destroyed.
    """
    cwd = params.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise Reject("params.cwd is required")
    if not os.path.isdir(cwd):
        raise Reject(f"cwd does not exist: {cwd}")
    target = params.get("target", "main")
    remote = params.get("remote", "origin")
    for name, val in (("target", target), ("remote", remote)):
        # These reach a command line; keep them boring.
        if (
            not isinstance(val, str)
            or not val.replace("-", "").replace("_", "").replace("/", "").isalnum()
        ):
            raise Reject(f"params.{name} must be a plain ref name, got {val!r}")

    steps: list[dict[str, Any]] = []

    def step(name: str, rc: int, out: str) -> bool:
        steps.append({"step": name, "rc": rc, "out": out[-4000:]})
        ctx.log(f"  {name}: rc={rc}")
        return rc == 0

    rc, root = git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0:
        raise Reject(f"not a git repository: {cwd}")
    root = root.strip()

    rc, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = branch.strip()

    # 1. Refuse a dirty tree. Landing half-written work is exactly the failure
    #    mode this whole system exists to prevent — but only *tracked* changes
    #    can end up in a commit, and therefore in the push. An untracked scratch
    #    file blocks nothing and blocking on it turns a correct refusal into
    #    noise: the requester reads "dirty", cannot see why it matters, and
    #    starts deleting things to get past the gate.
    rc, out = git(["status", "--porcelain"], root)
    if rc != 0:
        return _fail("could not read git status", steps)
    tracked = [ln for ln in out.splitlines() if not ln.startswith("??")]
    untracked = [ln for ln in out.splitlines() if ln.startswith("??")]
    if tracked:
        return _fail(
            "working tree has uncommitted tracked changes — commit or stash "
            "first. The daemon lands commits, it does not decide what to "
            f"commit:\n{chr(10).join(tracked)[:2000]}" + _claude_settings_hint(tracked),
            steps,
        )
    step(
        "status", 0, f"clean ({len(untracked)} untracked file(s) ignored)" if untracked else "clean"
    )

    # 2. Fetch.
    if not step("fetch", *git(["fetch", remote, target], root)):
        return _fail(f"fetch {remote}/{target} failed", steps)

    rc, ahead = git(["rev-list", "--count", f"{remote}/{target}..HEAD"], root)
    if rc == 0 and ahead.strip() == "0":
        return {
            "ok": True,
            "landed": False,
            "reason": f"nothing to land — HEAD has no commits beyond {remote}/{target}",
            "branch": branch,
            "steps": steps,
        }

    # 3. Rebase. On conflict, abort and hand it back: an automated conflict
    #    resolution is a guess, and guessing here silently rewrites someone's work.
    rc, out = git(["rebase", f"{remote}/{target}"], root)
    steps.append({"step": "rebase", "rc": rc, "out": out[-4000:]})
    if rc != 0:
        git(["rebase", "--abort"], root)
        return _fail(
            f"rebase onto {remote}/{target} conflicted — aborted, your branch is "
            f"untouched. Resolve it yourself:\n{out[:2000]}",
            steps,
        )
    # Verify the rebase did what it claims: rc=0 covers "rebased" and "did
    # nothing" alike, and landing un-rebased work is how two heads reach main.
    anc_rc, _ = git(["merge-base", "--is-ancestor", f"{remote}/{target}", "HEAD"], root)
    steps.append(
        {
            "step": "verify-rebase",
            "rc": anc_rc,
            "out": f"{remote}/{target} {'is' if anc_rc == 0 else 'is NOT'} an ancestor of HEAD",
        }
    )
    if anc_rc != 0:
        return _fail(
            f"rebase exited 0 but {remote}/{target} is still not an ancestor of "
            "HEAD — refusing to push work that isn't actually rebased.",
            steps,
        )

    # 4. Gate. Repo-owned. A gate that cannot run must FAIL, not skip: a gate
    #    that skips silently is worse than no gate, because it grants confidence
    #    nobody earned.
    gate = os.path.join(root, GATE_PATH)
    if os.path.exists(gate):
        if not os.access(gate, os.X_OK):
            return _fail(f"{GATE_PATH} exists but is not executable — refusing to land", steps)
        ctx.log(f"  running gate {GATE_PATH} (timeout {GATE_TIMEOUT}s)")
        rc, out = run([gate], root, timeout=GATE_TIMEOUT)
        steps.append({"step": "gate", "rc": rc, "out": out[-8000:]})
        if rc != 0:
            return _fail(f"gate failed (rc={rc}) — not pushing:\n{out[-2000:]}", steps)
        gate_state = "passed"
    else:
        # Loud, and recorded in the result, so "no gate" can never look like
        # "gate passed" to anyone reading the queue.
        gate_state = "ABSENT"
        steps.append(
            {"step": "gate", "rc": 0, "out": f"no {GATE_PATH} in {root} — NOTHING VERIFIED"}
        )
        ctx.log(f"  ⚠ no gate at {GATE_PATH} — landing unverified")

    # 5. Push.
    rc, sha = git(["rev-parse", "HEAD"], root)
    sha = sha.strip()
    if not ctx.allow_push:
        return {
            "ok": True,
            "landed": False,
            "reason": "dry run — everything up to push succeeded. Start the daemon "
            "with --allow-push to actually push.",
            "branch": branch,
            "gate": gate_state,
            "steps": steps,
        }
    push_rc, push_out = git(["push", remote, f"HEAD:{target}"], root)
    steps.append({"step": "push", "rc": push_rc, "out": push_out[-4000:]})

    # 6. Verify against the REMOTE, not against push's exit code.
    #
    # An exit code is the command's opinion of itself. `git push | tail` reports
    # tail's success; `git add A B` with one bad path stages nothing and the
    # commit still "succeeds". This whole system exists because components report
    # success they never earned, so the one place it must not happen is here.
    # ls-remote asks the remote what it actually has.
    ver_rc, ver_out = git(["ls-remote", remote, f"refs/heads/{target}"], root)
    if ver_rc != 0:
        # "I could not check" is not "I did not do it". Claiming a failed land
        # here is unearned certainty in the other direction, and it points the
        # requester at exactly the wrong recovery (re-push, re-rebase).
        steps.append(
            {
                "step": "verify",
                "rc": ver_rc,
                "out": f"ls-remote failed — landed state UNVERIFIED:\n{ver_out[-1000:]}",
            }
        )
        return {
            "ok": True,
            "landed": None,
            "reason": f"push reported rc={push_rc} but the remote could not be "
            "reached to verify — the land is UNVERIFIED, not failed. Check the "
            "remote yourself before retrying anything.",
            "branch": branch,
            "head": sha[:12],
            "target": f"{remote}/{target}",
            "gate": gate_state,
            "steps": steps,
        }
    remote_sha = ver_out.split()[0] if ver_out.split() else ""
    landed = remote_sha == sha
    detail = "tip MATCH"
    if not landed and remote_sha:
        # The tip moved — another push may have landed on top between ours and
        # this look. Tip equality answers "am I the newest commit?", which is
        # not the question. The question is "is my work on the branch?" —
        # answer it by ancestry against the tip the remote just reported.
        f_rc, _ = git(["fetch", remote, target], root)
        if f_rc == 0 and git(["merge-base", "--is-ancestor", sha, remote_sha], root)[0] == 0:
            landed = True
            detail = f"tip moved to {remote_sha[:12]}, our commit is an ancestor"
    steps.append(
        {
            "step": "verify",
            "rc": 0 if landed else 1,
            "out": f"{remote}/{target} is at {remote_sha[:12] or '?'}; "
            f"we pushed {sha[:12]}; {detail if landed else 'MISMATCH'}",
        }
    )
    if not landed:
        return _fail(
            f"push reported rc={push_rc} but {remote}/{target} is at "
            f"{remote_sha[:12] or '<unknown>'} and does not contain {sha[:12]} — "
            f"I did NOT land this. Do not trust the push output:\n{push_out[-1500:]}",
            steps,
        )

    return {
        "ok": True,
        "landed": True,
        "branch": branch,
        "head": sha[:12],
        "target": f"{remote}/{target}",
        "gate": gate_state,
        "verified": f"{remote}/{target} contains {sha[:12]} per ls-remote ({detail})",
        "steps": steps,
    }


def _fail(reason: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ok": False, "landed": False, "reason": reason, "steps": steps}


# --------------------------------------------------------------------------- #
# spawn
# --------------------------------------------------------------------------- #
# Every role shares this. The rules are stated as mechanism, not encouragement:
# each one exists because it was violated in a shared tree on 2026-07-15.
_COMMON = """
You are one agent in a fleet coordinated over the cabildo bus.

Your tree is yours. You are running in a dedicated git worktree on your own
branch. Nobody else edits these files. Work here freely; never `cd` to another
worktree or the main checkout to "just fix one thing".

You do not push. When your work is committed and you want it on {target},
call `request(action="land", params={{"cwd": "{worktree}"}})`. A single-threaded
daemon serialises fetch/rebase/gate/push, so your land cannot race anyone's. It
will DM you the result. If it fails, the failure is yours to fix, not to retry
blindly. Landing requires a CLEAN tree — commit first; the daemon will not
decide what to commit for you.

Talk on the bus: `send` to report or ask, `inbox` to read (messages are never
auto-read), `read` to mark. Use `urgent=True` ONLY when you are blocked and
someone must act — it prevents the recipient from ending their turn. Message
@jorge (the human) for a decision only he can make; it pops a notification on
his desktop, so make it worth interrupting him for.

Be brief on #general. A post there is read by every agent, so it costs several
times what it looks like. Detail goes in a DM to whoever needs it.

You were spawned by {parent}, and {parent} is owed your outcome. When the task
is done — landed through the daemon, or blocked for good — DM {parent} a short
report: what you did, where it stands, what they must know. Do not end your
final turn without sending it; a worker that vanishes silently costs more than
one that never started.

You may spawn helper agents for genuinely parallel subtasks:
`request(action="spawn", params={{"role": "qa", "task": "...", "repo": "..."}})`.
The daemon enforces the fleet cap, the usage budget, and a depth limit — its
refusal is an answer, not an obstacle to route around.

The win condition is shared: the gate passes on {target}. Not your own
scoreboard. You do not get to declare your own work done — the gate does.
"""

ROLES: dict[str, str] = {
    "pm": _COMMON
    + """
YOUR ROLE: PM / coordinator (@pm).
You own the work queue and sequencing: what is next, what is blocked, what is
duplicated. You do not write feature code.
Your value is preventing two agents from doing the same work or colliding on the
same scope — check `list_agents` and `claims` before assigning.
Beware your own failure mode: a PM that generates work to look busy is worse
than no PM. If nothing needs coordinating, say so and stop.
""",
    "qa": _COMMON
    + """
YOUR ROLE: QA / testing (@qa).
You own whether the gate can be TRUSTED, not the bug count. A gate that silently
skips is worse than no gate: it grants confidence nobody earned. Hunt for gates
that pass without verifying — a missing sibling repo, a skipped suite, an
unrunnable script — and make them fail loudly instead.
Your win condition is "the gate is trustworthy and green", never "bugs found".
Do not optimise for finding things; a QA agent rewarded for bugs will always
find bugs, and that is a local optimum that costs the team.
""",
    "ux": _COMMON
    + """
YOUR ROLE: UI / UX (@ux).
You own the interface and its visual verification.
Verify by driving the real thing, not by reasoning about it. Prefer bundling and
loading over file:// to standing up a server — a backgrounded dev server inside
a subshell is orphaned at birth and leaks forever. If you must run one, own its
teardown; two such servers ran for over two days on this machine.
Never reap a port you did not start: it is probably a shared stack someone else
is driving right now.
""",
}


# Model aliases the spawner accepts. Not every string claude accepts: a spawn
# spec is config, and config that passes through unvalidated becomes an
# injection point on a command line.
MODELS = {"haiku", "sonnet", "opus"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:40]


def _reconcile_live(conn) -> int:
    """Count live spawned agents from the ledger, marking dead ones as such.

    The ledger and not daemon memory, because daemon memory dies with the
    daemon: a restart would reset the fleet cap to zero while yesterday's
    agents are still burning tokens. A status row is a claim; the PID check is
    the audit.
    """
    from . import store

    live = 0
    for r in store.live_spawns(conn):
        pid = r["pid"]
        if pid:
            try:
                os.kill(pid, 0)
                live += 1
                continue
            except ProcessLookupError:
                pass
            except PermissionError:
                # The PID exists but is not ours — somebody is home. PID reuse
                # can make this a stranger; counting a stranger against the cap
                # errs on the side of spawning less, which is the safe side.
                live += 1
                continue
        store.mark_spawn_dead(conn, r["tag"])
    return live


def _throttle(ctx: Ctx) -> dict[str, Any] | None:
    """Refuse to spawn when the trailing burn is already over budget. The
    spawner is the only place new load originates, so it is the only place a
    throttle actually bites.
    """
    if not ctx.budget:
        return None
    from . import usage  # local: keeps the transcript scan off the import path

    s = usage.summarize(time.time() - ctx.budget_days * 86400)
    spent = s["totals"].get("weighted", 0.0)
    f = usage.forecast(spent, ctx.budget, usage.rate_per_hour(s["by_hour"]))
    if f["state"] in ("OVER", "CRITICAL"):
        return {
            "ok": False,
            "reason": f"throttled: {f['pct']:.1f}% of a {ctx.budget:,.0f} weighted-token "
            f"budget over {ctx.budget_days:g}d ({f['state']}). "
            + (
                f"At the current rate you cross it in {f['hours_left']:.1f}h (~{f['eta']}). "
                if f["hours_left"]
                else ""
            )
            + "Spawning more agents now would burn the rest faster. Not spawning.",
            "usage": f,
        }
    return None


def spawn(params: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Create an isolated worktree and launch a headless agent in it.

    Isolation is not advice here. The only way to get an agent is to get one in
    its own tree, so there is nothing to remember and nothing to defect from.
    """
    from . import store  # local: actions.py stays importable headless

    role = params.get("role")
    task = params.get("task")
    repo = params.get("repo")
    target = params.get("target", "main")
    model = params.get("model")
    # Stamped by the MCP server / CLI from the resolved identity, never claimed
    # by the caller. Whoever asked is the parent; the child reports back to it.
    parent = params.get("_from_session") or store.HUMAN_SESSION
    if role not in ROLES:
        raise Reject(f"unknown role {role!r}; allowed: {sorted(ROLES)}")
    if not isinstance(task, str) or len(task.strip()) < 10:
        raise Reject("params.task must be a real instruction (>=10 chars)")
    if not isinstance(repo, str) or not os.path.isdir(repo):
        raise Reject(f"params.repo is not a directory: {repo!r}")
    if not target.replace("-", "").replace("_", "").replace("/", "").isalnum():
        raise Reject(f"params.target must be a plain ref name, got {target!r}")
    if model is not None and model not in MODELS:
        raise Reject(f"unknown model {model!r}; allowed: {sorted(MODELS)}, or omit for the default")

    conn = store.connect()
    try:
        # Lineage first: depth comes from the ledger, and the guard sits at the
        # single choke point every spawn must pass. There is no second door.
        depth = store.spawn_depth_of(conn, parent) + 1
        if depth > ctx.max_depth:
            return {
                "ok": False,
                "reason": f"spawn depth {depth} exceeds the limit ({ctx.max_depth}). "
                "You are too deep in the tree to delegate further — do the "
                "work yourself, or DM your spawner to ask for help.",
            }

        live = _reconcile_live(conn)
        if live >= ctx.max_agents:
            return {
                "ok": False,
                "reason": f"at the {ctx.max_agents}-agent cap; {live} still running. Not spawning.",
            }

        throttled = _throttle(ctx)
        if throttled:
            ctx.log(f"  ⛔ {throttled['reason'][:90]}")
            return throttled

        claude = shutil.which("claude")
        if not claude:
            raise Reject("no `claude` on PATH — cannot spawn")

        rc, root = git(["rev-parse", "--show-toplevel"], repo)
        if rc != 0:
            raise Reject(f"not a git repository: {repo}")
        root = root.strip()

        stamp = time.strftime("%m%d-%H%M%S")
        branch = f"agent/{role}/{stamp}"
        wt = os.path.join(os.path.dirname(root), WORKTREE_DIRNAME, f"{_slug(role)}-{stamp}")
        os.makedirs(os.path.dirname(wt), exist_ok=True)

        rc, out = git(["fetch", "origin", target], root)
        base_ref = f"origin/{target}" if rc == 0 else target
        rc, out = git(["worktree", "add", "-b", branch, wt, base_ref], root)
        if rc != 0:
            return {"ok": False, "reason": f"could not create worktree: {out[:800]}"}

        if not ctx.allow_spawn:
            git(["worktree", "remove", "--force", wt], root)
            git(["branch", "-D", branch], root)
            return {
                "ok": True,
                "spawned": False,
                "reason": "dry run — worktree created and removed cleanly; would have "
                f"launched {role} in {wt}. Start the daemon with --allow-spawn to "
                "actually launch agents.",
                "would_run": f"claude -p <task> (role={role}, "
                f"model={model or 'default'}, depth={depth}, cwd={wt})",
            }

        parent_label = store.display_name(store.get_session(conn, parent), parent)
        prompt = ROLES[role].format(target=target, worktree=wt, parent=parent_label)
        log_path = os.path.join(wt, f".agent-{role}.log")
        cmd = [
            claude,
            "-p",
            task,
            "--append-system-prompt",
            prompt,
            "--permission-mode",
            "acceptEdits",
            # acceptEdits covers file edits but NOT mcp tools, and a headless agent
            # cannot answer a permission prompt: without this it boots, registers on
            # the bus, and is then mute — which is exactly what happened the first
            # time. Named explicitly rather than a wildcard: a spawned agent gets a
            # voice, not the run of the machine.
            "--allowedTools",
            ",".join(BUS_TOOLS),
            "--add-dir",
            root,
        ]
        if model:
            cmd += ["--model", model]
        # The agent's identity, decided here rather than inferred later. Both its
        # SessionStart hook and its MCP server inherit this from the same process,
        # so they cannot disagree about who it is.
        tag = f"spawn-{role}-{stamp}-{os.getpid()}"
        # Ledger row goes in BEFORE the process starts: the child's SessionStart
        # backfills it by tag, and a row that arrives after the child registers
        # would lose that race.
        store.record_spawn(
            conn,
            tag=tag,
            parent_session=parent,
            role=role,
            task=task,
            model=model,
            depth=depth,
            worktree=wt,
            branch=branch,
            log=log_path,
        )
        logf = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd,
            cwd=wt,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives the daemon; it is not our child to babysit
            env={**os.environ, "CLAUDE_PROJECT_DIR": wt, "CABILDO_TAG": tag},
        )
        store.set_spawn_pid(conn, tag, proc.pid)
        ctx.log(
            f"  🚀 launched {role} pid={proc.pid} (depth {depth}, "
            f"for {parent_label}) — verifying it's really alive…"
        )

        alive, sid, why = _verify_spawn(proc, wt, ctx, tag=tag)
        base = {
            "role": role,
            "pid": proc.pid,
            "worktree": wt,
            "branch": branch,
            "log": log_path,
            "parent": parent_label,
            "depth": depth,
            "model": model or "default",
        }
        if not alive:
            # Popen succeeding means the OS forked, nothing more. The first spawn
            # this daemon ever did "succeeded" while the agent sat there mute and
            # exited: every mcp call denied, no permission prompt possible. Reporting
            # that as success is the exact failure this system exists to catch.
            store.mark_spawn_dead(conn, tag)
            return {
                **base,
                "ok": False,
                "spawned": False,
                "reason": f"launched but did not survive: {why}. Last words:\n"
                f"{_tail(log_path, 900)}",
            }
        return {
            **base,
            "ok": True,
            "spawned": True,
            "session": sid,
            "verified": f"registered on the bus as {(sid or '?')[:8]}"
            if sid
            else "process alive, but it has NOT registered on the bus",
            "reason": f"{role} is running in its own worktree on {branch}, "
            f"reporting to {parent_label}."
            + (
                ""
                if sid
                else " NOTE: it has not announced itself on the "
                "bus — it may lack permission to use the bus tools, in "
                "which case it can work but cannot talk."
            ),
        }
    finally:
        conn.close()


def _tail(path: str, n: int) -> str:
    try:
        with open(path, "rb") as f:
            return f.read()[-n:].decode(errors="replace").strip() or "(empty)"
    except OSError:
        return "(no log)"


def _verify_spawn(
    proc, wt: str, ctx: Ctx, *, tag: str, timeout: int = 25
) -> tuple[bool, str | None, str]:
    """Did it actually start, and did it reach the bus as the agent we launched?

    Two independent checks, because they fail differently: the process being
    alive says the binary ran; a row carrying OUR tag says its SessionStart hook
    fired and bound the identity we handed it. Matching on cwd instead would
    happily find a leftover row from a previous agent in a reused worktree and
    report someone else's success as ours.
    """
    from . import store

    conn = store.connect()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                return False, None, f"exited rc={proc.returncode} within {timeout}s"
            sid = store.session_for_tag(conn, tag)
            if sid:
                return True, sid, "registered"
            time.sleep(1)
        return True, None, "alive but never registered on the bus"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# ask
# --------------------------------------------------------------------------- #
def ask(params: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Put a question to the human as a desktop notification with buttons, and
    post his answer back to the asker as a DM from @jorge.

    Returns as soon as the notification is up. The human may take ten minutes;
    the queue is single-threaded and must keep draining, so the wait happens on
    a thread and the answer arrives on the bus, asynchronously, like everything
    else here.
    """
    from . import desktop, store  # local: actions.py stays importable headless

    question = params.get("question")
    options = params.get("options") or ["yes", "no"]
    context = params.get("context") or ""
    asker = params.get("_from_session")
    asker_name = params.get("_from_handle") or "an agent"
    project = params.get("_project") or ""

    if not isinstance(question, str) or len(question.strip()) < 5:
        raise Reject("params.question must be a real question (>=5 chars)")
    if not isinstance(options, list) or not (1 <= len(options) <= 4):
        raise Reject("params.options must be a list of 1-4 short strings")
    options = [str(o)[:24] for o in options]
    if not desktop.available():
        return {
            "ok": False,
            "reason": "no desktop notifications available here "
            "(no session bus / no notify-send) — ask on the bus instead",
        }

    # Everything the human needs to answer without hunting for a terminal.
    where = os.path.basename(project.rstrip("/")) if project else "?"
    summary = f"❓ {asker_name} needs your call — {where}"
    body = question if not context else f"{question}\n\n{context}"

    up = threading.Event()

    def wait_and_post() -> None:
        # ask() blocks until clicked, so "did it appear?" can only be answered
        # from in here. Claiming the popup is up before it is would be the same
        # unearned success this action exists to route around.
        up.set()
        choice = desktop.ask(summary, body, options)
        conn = store.connect()
        try:
            if choice is None:
                store.post_message(
                    conn,
                    from_session=store.HUMAN_SESSION,
                    from_handle=store.HUMAN_HANDLE,
                    body=f"(no answer — the notification for {question!r} was dismissed. "
                    "Ask again on the bus if you're still blocked.)",
                    to_session=asker,
                    project=project or None,
                    urgent=False,
                )
            else:
                store.post_message(
                    conn,
                    from_session=store.HUMAN_SESSION,
                    from_handle=store.HUMAN_HANDLE,
                    body=f"{choice}\n\n(in answer to: {question})",
                    to_session=asker,
                    project=project or None,
                    # Urgent so the asker cannot idle through the answer it asked for.
                    urgent=True,
                )
            ctx.log(f"  💬 jorge answered {choice!r} to {asker_name}")
        finally:
            conn.close()

    t = threading.Thread(target=wait_and_post, daemon=True)
    t.start()
    if not up.wait(timeout=5):
        return {
            "ok": False,
            "asked": False,
            "reason": "could not get the question in front of him — the "
            "notifier thread never started. Ask on the bus instead.",
        }
    return {
        "ok": True,
        "asked": True,
        "question": question,
        "options": options,
        "reason": "the question is in front of him; his answer will arrive as an "
        "urgent DM from @jorge whenever he clicks. Do not block on it — "
        "go do something else and read your inbox. If he dismisses it "
        "you get told that too, rather than silence.",
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
ACTIONS: dict[str, Callable[[dict[str, Any], Ctx], dict[str, Any]]] = {
    "land": land,
    "spawn": spawn,
    "ask": ask,
}
