"""The deterministic entity: drains the intents queue, one action at a time.

Why this is single-threaded and unapologetic about it: landing is a serial
operation, and the strongest lock is not needing one. A single worker draining a
queue cannot race itself, so there is no lease to expire, no lock to leak, and
no rebase-under-you. Concurrency here would buy nothing and cost correctness.

    python -m cabildo.daemon                 # dry run: everything but the push
    python -m cabildo.daemon --allow-push    # actually push
    python -m cabildo.daemon --once          # drain what's queued, then exit

Results go back three ways: the intent row (authoritative), a line on stdout,
and a message on the bus to the requester (so an agent learns its land failed
without polling).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

from . import actions, desktop, names, store

# A 'running' intent older than this means the daemon died holding it.
STALE_AFTER = actions.GATE_TIMEOUT + 300
# How often the poll loop sweeps for sessions whose owning claude died without
# firing the SessionEnd hook. Cheap (one os.kill per active session) but not
# worth doing on every 2s tick.
REAP_SESSIONS_EVERY = 60.0

DAEMON_SESSION = "daemon"
DAEMON_HANDLE = "daemon"


def _pending_for_human(conn, after_id: int) -> list[dict]:
    """Messages the human should be interrupted for: anything sent to him
    directly, plus anything flagged urgent anywhere. Never the daemon's own
    chatter, and never his own messages echoed back."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE id > ? AND from_session != ? "
        "AND (to_session = ? OR urgent = 1) ORDER BY id",
        (after_id, store.HUMAN_SESSION, store.HUMAN_SESSION),
    ).fetchall()
    return [store._msg_dict(r) for r in rows]


def _worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _report(conn, intent: dict, status: str, result: dict) -> None:
    """Tell the requester what happened, on the bus, as a DM."""
    ok = result.get("ok")
    head = "⌛" if result.get("expired") else (
        "✅" if (ok and result.get("landed")) else ("⚠️" if ok else "❌"))
    body = [
        f"{head} intent #{intent['id']} `{intent['action']}` → {status}",
        result.get("reason") or "",
    ]
    if result.get("landed"):
        body.append(f"landed {result.get('head')} on {result.get('target')} (gate: {result.get('gate')})")
    if result.get("gate") == "ABSENT":
        body.append(
            f"⚠ NOTHING WAS VERIFIED: no {actions.GATE_PATH} in this repo. The "
            "daemon pushed on your word alone."
        )
    try:
        store.post_message(
            conn,
            from_session=DAEMON_SESSION,
            from_handle=DAEMON_HANDLE,
            body="\n".join(b for b in body if b),
            to_session=intent["from_session"],
            project=intent.get("project"),
            # A failed land blocks that agent from idling until it looks. An
            # EXPIRED one does not: urgent mail also pops a desktop notification
            # for the human, and a week-old request nobody is waiting on must not
            # buy itself a popup.
            urgent=not ok and not result.get("expired"),
        )
    except Exception as e:  # a broken report must not poison the queue
        _log(f"  (could not post result: {e})")


def _age(intent: dict) -> float:
    return max(0.0, time.time() - float(intent.get("created_at") or 0))


def handle(conn, intent: dict, ctx: actions.Ctx) -> None:
    # An intent is a request about a moment. By the time a daemon comes up after
    # a day off, the target branch has moved, the tree it described is gone, and
    # the agent that asked is dead — so executing the backlog is not obedience,
    # it is acting on stale facts nobody is watching. Refuse, and say why.
    age = _age(intent)
    if ctx.max_intent_age and age > ctx.max_intent_age:
        result = {
            "ok": False,
            "expired": True,
            "reason": f"expired: queued {age / 3600:.1f}h ago, ceiling is "
                      f"{ctx.max_intent_age / 3600:.1f}h. Nothing was run — the "
                      f"tree and the target branch have moved since. Re-request "
                      f"it if you still want it.",
        }
        store.finish_intent(conn, intent["id"], status="expired", result=result)
        _log(f"⌛ #{intent['id']} {intent['action']} expired ({age / 3600:.1f}h old)")
        _report(conn, intent, "expired", result)
        return

    fn = actions.ACTIONS.get(intent["action"])
    if fn is None:
        result = {
            "ok": False,
            "reason": f"unknown action {intent['action']!r}; allowed: "
            f"{sorted(actions.ACTIONS)}",
        }
        store.finish_intent(conn, intent["id"], status="rejected", result=result)
        _report(conn, intent, "rejected", result)
        return

    _log(f"▶ #{intent['id']} {intent['action']} from {intent['from_handle'] or names.robot_name(intent['from_session'])}")
    try:
        result = fn(intent["params"], ctx)
        status = "done" if result.get("ok") else "failed"
    except actions.Reject as e:
        result, status = {"ok": False, "reason": str(e)}, "rejected"
    except Exception as e:  # never let one bad intent kill the daemon
        result, status = {"ok": False, "reason": f"{type(e).__name__}: {e}"}, "failed"

    store.finish_intent(conn, intent["id"], status=status, result=result)
    _log(f"■ #{intent['id']} {status}: {str(result.get('reason') or '')[:120]}")
    _report(conn, intent, status, result)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cabildo-daemon", description=(__doc__ or "").split("\n")[0])
    p.add_argument("--allow-push", action="store_true",
                   help="actually push. Without it, land stops before the push and reports.")
    p.add_argument("--allow-spawn", action="store_true",
                   help="actually launch agents. Without it, spawn builds the worktree, "
                        "proves it works, and cleans up.")
    p.add_argument("--max-agents", type=int, default=3, help="concurrent spawned agents")
    p.add_argument("--max-depth", type=int, default=2,
                   help="how deep spawn chains may go (you→agent is depth 1, "
                        "its helper is 2)")
    p.add_argument("--budget", type=float, default=0.0,
                   help="weighted-token ceiling over --budget-days; refuse to spawn past it. "
                        "0 disables the throttle.")
    p.add_argument("--budget-days", type=float, default=7.0, help="window for --budget")
    p.add_argument("--max-intent-age", type=float, default=2.0,
                   help="hours a queued intent may wait before the daemon refuses "
                        "it instead of running it (0 disables)")
    p.add_argument("--once", action="store_true", help="drain the queue and exit")
    p.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    p.add_argument("--no-notify", action="store_true",
                   help="don't pop desktop notifications for messages addressed to you")
    args = p.parse_args(argv)

    worker = _worker_name()
    conn = store.connect()
    store.register_session(conn, DAEMON_SESSION, cwd=os.getcwd())
    store.set_handle(conn, DAEMON_SESSION, DAEMON_HANDLE)

    n = store.reap_stale_intents(conn, STALE_AFTER)
    if n:
        _log(f"failed {n} stale intent(s) left 'running' by a dead daemon")

    ctx = actions.Ctx(
        worker=worker,
        allow_push=args.allow_push,
        allow_spawn=args.allow_spawn,
        max_agents=args.max_agents,
        max_depth=args.max_depth,
        budget=args.budget,
        budget_days=args.budget_days,
        max_intent_age=args.max_intent_age * 3600.0,
        log=_log,
    )

    notifying = not args.no_notify and desktop.available()
    if not args.no_notify and not notifying:
        _log("desktop notifications unavailable (no session bus or no notify-send)")
    # Only notify about what arrives from now on: replaying a day of backlog as
    # popups the moment the daemon starts is a good way to get it turned off.
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM messages").fetchone()
    last_msg = row["m"]

    _log(f"daemon {worker} up · actions={sorted(actions.ACTIONS)} · "
         f"push={'ENABLED' if args.allow_push else 'dry-run'} · "
         f"spawn={'ENABLED' if args.allow_spawn else 'dry-run'} "
         f"(≤{args.max_agents} agents, depth ≤{args.max_depth}) · "
         f"notify={'on' if notifying else 'off'} · "
         f"budget={'%.0f/%gd' % (args.budget, args.budget_days) if args.budget else 'none'} · "
         f"intent age ≤{'%gh' % args.max_intent_age if args.max_intent_age else 'unbounded'}")

    # What is already in the queue, before touching any of it. A daemon coming up
    # with push enabled onto a backlog it did not queue is the moment to be loud.
    waiting = store.count_pending_intents(conn)
    if waiting:
        stale = store.count_pending_intents(conn, older_than=ctx.max_intent_age)
        fresh = waiting - stale if ctx.max_intent_age else waiting
        _log(f"queue: {waiting} pending — {stale} will be refused as expired, "
             f"{fresh} will be RUN")

    last_reap = 0.0
    try:
        while True:
            intent = store.take_next_intent(conn, worker)
            if intent is not None:
                handle(conn, intent, ctx)
                continue
            if notifying:
                for m in _pending_for_human(conn, last_msg):
                    last_msg = m["id"]
                    if desktop.notify_message(m):
                        _log(f"🔔 {m['from_display']}: {str(m['body'])[:60]!r}")
            now = time.monotonic()
            if now - last_reap >= REAP_SESSIONS_EVERY:
                last_reap = now
                dead = store.reap_dead_sessions(conn)
                if dead:
                    _log(f"reaped {dead} session(s) whose claude process is gone")
            if args.once:
                _log("queue empty — exiting")
                return 0
            store.touch(conn, DAEMON_SESSION)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _log("bye")
        return 0


if __name__ == "__main__":
    sys.exit(main())
