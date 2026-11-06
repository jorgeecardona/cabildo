"""The deterministic entity: drains the intents queue, one action per lane at a time.

Pushing is a serial operation per repo: two pushes from one repo race (rebase
under rebase, index.lock). Pushes from different repos cannot conflict, so each
repo is its own lane and lanes run in parallel on worker threads.
``store.take_next_intent`` never hands out a second intent from a busy lane,
in one atomic UPDATE, so there is no lease to expire.

    python -m cabildo.daemon                 # dry run: everything but the push
    python -m cabildo.daemon --allow-push    # actually push
    python -m cabildo.daemon --once          # drain what's queued, then exit

Results go back three ways: the intent row (authoritative), a line on stdout,
and a message on the bus to the requester (so an agent learns its push failed
without polling).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from . import (
    actions,
    broker,
    ci_watch,
    config,
    desktop,
    gitlab,
    lan,
    monitor,
    names,
    record,
    store,
    wan,
)

# A 'running' intent older than this means the daemon died holding it.
STALE_AFTER = actions.GATE_TIMEOUT + 300
# How long exit waits for running actions before failing them.
WORKER_JOIN_TIMEOUT = 30.0
# How often the poll loop sweeps for sessions whose owning claude died without
# firing the SessionEnd hook. Cheap (one os.kill per active session) but not
# worth doing on every 2s tick.
REAP_SESSIONS_EVERY = 60.0
# How often to sweep opted-in transcripts into the DB. Cheap (each pass reads
# only the new bytes since the last cursor), so a short cadence keeps the record
# close to live without polling the queue any harder.
RECORD_MESSAGES_EVERY = 15.0
# How often to sample the machine. A /proc walk is ~10ms, but the conditions it
# raises are told to agents once per episode regardless of sample rate, so
# sampling faster buys reaction time and costs nobody context.
MONITOR_EVERY = 30.0

DAEMON_SESSION = "daemon"
DAEMON_HANDLE = "daemon"


def _pending_for_human(conn, after_id: int, *, everything: bool = False) -> list[dict]:
    """Messages worth interrupting the human for: the ones addressed to him.

    It used to be "addressed to him, OR urgent anywhere", and that second clause was the whole
    problem. `urgent` on the bus means "interrupt the recipient's turn" — it is a property of a
    message to *somebody*, and when that somebody is another agent it is not news for this desk.
    Measured over the last 40 desktop pops: **none** were addressed to him. Twenty were the daemon
    telling an agent its push failed; twenty were five agents on #proj:paas working out which of
    them was writing docs/issues/. All of it was the bus doing its job, and all of it arrived as a
    popup he could not act on.

    An agent that genuinely needs him has a channel that says so — `request(action="ask")`, which
    pops with buttons and waits for an answer. Everything else is readable in `cabildo watch`,
    which is where you go when you want the firehose. `--notify-all` restores the old behaviour for
    anyone who does.
    """
    if everything:
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? AND from_session != ? "
            "AND (to_session = ? OR urgent = 1) ORDER BY id",
            (after_id, store.HUMAN_SESSION, store.HUMAN_SESSION),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? AND from_session != ? AND to_session = ? "
            "ORDER BY id",
            (after_id, store.HUMAN_SESSION, store.HUMAN_SESSION),
        ).fetchall()
    return [store._msg_dict(r) for r in rows]


def _worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _reap_dead_workers(conn) -> int:
    """Fail 'running' intents taken by a daemon on this host whose process is gone.

    A restart (systemd stop, a crash) can skip the exit-time reap, and the age-based
    reap waits ``STALE_AFTER`` (35 min) while the row blocks its lane.
    """
    host = socket.gethostname()
    n = 0
    for (w,) in conn.execute(
        "SELECT DISTINCT worker FROM intents WHERE status='running' AND worker LIKE ?",
        (host + ":%",),
    ).fetchall():
        pid = str(w).rsplit(":", 1)[-1]
        if pid.isdigit() and int(pid) != os.getpid() and not Path(f"/proc/{pid}").exists():
            n += store.reap_stale_intents(conn, 0, worker=str(w))
    return n


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _report(conn, intent: dict, status: str, result: dict) -> None:
    """Tell the requester what happened, on the bus, as a DM."""
    ok = result.get("ok")
    head = (
        "⌛"
        if result.get("expired")
        else ("✅" if (ok and result.get("pushed")) else ("⚠️" if ok else "❌"))
    )
    body = [
        f"{head} intent #{intent['id']} `{intent['action']}` → {status}",
        result.get("reason") or "",
    ]
    if result.get("pushed"):
        body.append(
            f"pushed {result.get('head')} on {result.get('target')} (gate: {result.get('gate')})"
        )
    if result.get("pushed") and result.get("gate") == "ABSENT":
        # Only for a real push: on a dry run nothing was pushed, so "pushed on
        # your word alone" would be doubly wrong (issue #15).
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
            # A failed push blocks that agent from idling until it looks. An
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
            "reason": f"unknown action {intent['action']!r}; allowed: {sorted(actions.ACTIONS)}",
        }
        store.finish_intent(conn, intent["id"], status="rejected", result=result)
        _report(conn, intent, "rejected", result)
        return

    _log(
        f"▶ #{intent['id']} {intent['action']} from {intent['from_handle'] or names.robot_name(intent['from_session'])}"
    )
    try:
        result = fn(intent["params"], ctx)
        # An action may name its own terminal status (e.g. push distinguishes a
        # dry run from a real pushing, so `done` always means "on the remote").
        status = result.get("status") or ("done" if result.get("ok") else "failed")
    except actions.Reject as e:
        result, status = {"ok": False, "reason": str(e)}, "rejected"
    except Exception as e:  # never let one bad intent kill the daemon
        result, status = {"ok": False, "reason": f"{type(e).__name__}: {e}"}, "failed"

    store.finish_intent(conn, intent["id"], status=status, result=result)
    _log(f"■ #{intent['id']} {status}: {str(result.get('reason') or '')[:120]}")
    _report(conn, intent, status, result)
    _start_ci_watch(intent, result)


def _start_ci_watch(intent: dict, result: dict) -> None:
    """After a verified push, report a failed GitLab pipeline to the requester on a thread."""
    sha, url = result.get("sha"), result.get("remote_url")
    if result.get("pushed") is not True or not sha or not url:
        return
    if config.ci_poll() > 0:
        return  # the GitLab poller reports this pipeline, to the same pusher
    timeout = config.push_ci_watch(str(intent["params"].get("cwd") or "") or None)
    if timeout <= 0:
        return
    threading.Thread(
        target=_ci_watch_thread, args=(intent, str(url), str(sha), timeout), daemon=True
    ).start()


def _ci_watch_thread(intent: dict, url: str, sha: str, timeout: float) -> None:
    try:
        body = ci_watch.watch(url, sha, timeout=timeout, glab=gitlab.api)
        if body is None:
            return
        conn = store.connect()
        try:
            store.post_message(
                conn,
                from_session=DAEMON_SESSION,
                from_handle=DAEMON_HANDLE,
                body=body,
                to_session=intent["from_session"],
                project=intent.get("project"),
                urgent=True,
            )
        finally:
            conn.close()
    except Exception as e:  # a broken watch must not touch the daemon
        _log(f"  (ci watch for {sha[:12]} failed: {type(e).__name__}: {e})")


def _ci_poll_thread() -> None:
    try:
        conn = store.connect()
        try:
            gitlab.poll(conn, log=_log)
        finally:
            conn.close()
    except Exception as e:  # a broken poll must not touch the daemon
        _log(f"gitlab poll failed: {type(e).__name__}: {e}")


def _handle_on_thread(intent: dict, ctx: actions.Ctx) -> None:
    """Run one intent on a worker thread; sqlite connections are per-thread.

    Any error before ``handle`` finishes the row (opening the DB can time out
    under write load) fails the intent here: a row left 'running' blocks its lane.
    """
    try:
        conn = store.connect()
        try:
            handle(conn, intent, ctx)
        finally:
            conn.close()
    except Exception as e:
        _log(f"■ #{intent['id']} failed outside the action: {type(e).__name__}: {e}")
        try:
            conn = store.connect()
            try:
                row = conn.execute(
                    "SELECT status FROM intents WHERE id=?", (intent["id"],)
                ).fetchone()
                if row is not None and row["status"] == "running":
                    store.finish_intent(
                        conn,
                        intent["id"],
                        status="failed",
                        result={"ok": False, "reason": f"{type(e).__name__}: {e}"},
                    )
            finally:
                conn.close()
        except Exception as e2:
            _log(f"■ #{intent['id']} could not be marked failed: {e2}")


def _run_broker(address: str, stop: threading.Event, ready: threading.Event) -> None:
    """Serve the broker socket, logging a bind failure instead of dying silently."""
    try:
        broker.serve(address, ready=ready, stop=stop)
    except Exception as e:  # a broker that cannot bind must not take the daemon down
        _log(f"broker: cannot serve on {address}: {type(e).__name__}: {e}")


def info_path() -> Path:
    """Where the running daemon records which build it is, for ``cabildo doctor``."""
    return store.home() / "daemon.json"


def _write_info() -> None:
    from . import __version__

    info = {
        "pid": os.getpid(),
        "version": __version__,
        "exe": sys.executable,
        "started_at": time.time(),
    }
    try:
        info_path().write_text(json.dumps(info))
    except OSError as e:  # doctor then says "unknown"; the daemon does not need this
        _log(f"cannot write {info_path()}: {e}")


def main(argv: list[str] | None = None, *, stop: threading.Event | None = None) -> int:
    p = argparse.ArgumentParser(prog="cabildo-daemon", description=(__doc__ or "").split("\n")[0])
    p.add_argument(
        "--allow-push",
        action="store_true",
        help="actually push. Without it, push stops before the push and reports.",
    )
    p.add_argument(
        "--allow-spawn",
        action="store_true",
        help="actually launch agents. Without it, spawn builds the worktree, "
        "proves it works, and cleans up.",
    )
    p.add_argument("--max-agents", type=int, default=3, help="concurrent spawned agents")
    p.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="how deep spawn chains may go (you→agent is depth 1, its helper is 2)",
    )
    p.add_argument(
        "--budget",
        type=float,
        default=0.0,
        help="weighted-token ceiling over --budget-days; refuse to spawn past it. "
        "0 disables the throttle.",
    )
    p.add_argument("--budget-days", type=float, default=7.0, help="window for --budget")
    p.add_argument(
        "--max-intent-age",
        type=float,
        default=2.0,
        help="hours a queued intent may wait before the daemon refuses "
        "it instead of running it (0 disables)",
    )
    p.add_argument(
        "--save-messages",
        action="store_true",
        help="record conversation turns into the bus DB for projects that set "
        "[record] messages = true. Off by default; text stays local (never uplinked).",
    )
    p.add_argument("--once", action="store_true", help="drain the queue and exit")
    p.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    p.add_argument(
        "--notify-all",
        action="store_true",
        help="pop a desktop notification for urgent traffic between agents too. "
        "Off by default: measured over 40 pops, none were addressed to the "
        "human. Use `cabildo watch` for the firehose.",
    )
    p.add_argument(
        "--no-notify",
        action="store_true",
        help="don't pop desktop notifications for messages addressed to you",
    )
    p.add_argument(
        "--no-monitor",
        action="store_true",
        help="don't watch this machine's memory / leftover processes",
    )
    p.add_argument(
        "--monitor-every",
        type=float,
        default=MONITOR_EVERY,
        help=f"seconds between machine samples (default: {MONITOR_EVERY:g})",
    )
    p.add_argument(
        "--mem-warn",
        type=float,
        default=monitor.Thresholds.warn_pct,
        help="%% RAM available below which agents are told to be careful",
    )
    p.add_argument(
        "--mem-critical",
        type=float,
        default=monitor.Thresholds.critical_pct,
        help="%% RAM available below which agents are told to stop starting things",
    )
    p.add_argument(
        "--orphan-mb",
        type=int,
        default=monitor.Thresholds.orphan_rss_mb,
        help="ignore leftover processes smaller than this",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="intents run at the same time, at most one per lane (a push per project)",
    )
    p.add_argument(
        "--broker",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="serve the bus over a local unix socket so MCP/hook clients can use it "
        "instead of opening the DB directly (default on; --no-broker to disable)",
    )
    p.add_argument(
        "--broker-socket",
        default=broker.socket_path(),
        help="unix socket path for the broker (default: <home>/bus.sock)",
    )
    args = p.parse_args(argv)
    if args.workers < 1:
        p.error("--workers must be at least 1")

    worker = _worker_name()
    conn = store.connect()
    store.register_session(conn, DAEMON_SESSION, cwd=os.getcwd())
    store.set_handle(conn, DAEMON_SESSION, DAEMON_HANDLE)

    n = store.reap_stale_intents(conn, STALE_AFTER) + _reap_dead_workers(conn)
    if n:
        _log(f"failed {n} stale intent(s) left 'running' by a dead daemon")
    store.join_room_all(conn)

    ctx = actions.Ctx(
        worker=worker,
        allow_push=args.allow_push,
        allow_spawn=args.allow_spawn,
        max_agents=args.max_agents,
        max_depth=args.max_depth,
        budget=args.budget,
        budget_days=args.budget_days,
        max_intent_age=args.max_intent_age * 3600.0,
        save_messages=args.save_messages,
        log=_log,
    )

    notifying = not args.no_notify and desktop.available()
    if not args.no_notify and not notifying:
        _log("desktop notifications unavailable (no session bus or no notify-send)")
    # Only notify about what arrives from now on: replaying a day of backlog as
    # popups the moment the daemon starts is a good way to get it turned off.
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM messages").fetchone()
    last_msg = row["m"]

    thresholds = monitor.Thresholds(
        warn_pct=args.mem_warn,
        critical_pct=args.mem_critical,
        orphan_rss_mb=args.orphan_mb,
    )

    _log(
        f"daemon {worker} up · actions={sorted(actions.ACTIONS)} · "
        f"push={'ENABLED' if args.allow_push else 'dry-run'} · "
        f"spawn={'ENABLED' if args.allow_spawn else 'dry-run'} "
        f"(≤{args.max_agents} agents, depth ≤{args.max_depth}) · "
        f"notify={'on' if notifying else 'off'} · "
        f"record={'ON' if args.save_messages else 'off'} · "
        f"monitor={'off' if args.no_monitor else 'mem<%g%%/%g%% every %gs' % (args.mem_warn, args.mem_critical, args.monitor_every)} · "
        f"budget={'%.0f/%gd' % (args.budget, args.budget_days) if args.budget else 'none'} · "
        f"intent age ≤{'%gh' % args.max_intent_age if args.max_intent_age else 'unbounded'}"
    )

    # What is already in the queue, before touching any of it. A daemon coming up
    # with push enabled onto a backlog it did not queue is the moment to be loud.
    waiting = store.count_pending_intents(conn)
    if waiting:
        stale = store.count_pending_intents(conn, older_than=ctx.max_intent_age)
        fresh = waiting - stale if ctx.max_intent_age else waiting
        _log(f"queue: {waiting} pending — {stale} will be refused as expired, {fresh} will be RUN")

    # The daemon owns the DB, so it is also the broker: serve the local socket on
    # its own thread (with its own connection) so MCP/hook clients can route
    # through it. --once is a drain-and-exit run, so it never bothers.
    broker_stop = threading.Event()
    broker_thread: threading.Thread | None = None
    if args.broker and not args.once:
        ready = threading.Event()
        broker_thread = threading.Thread(
            target=_run_broker, args=(args.broker_socket, broker_stop, ready), daemon=True
        )
        broker_thread.start()
        _log(
            f"broker listening on {args.broker_socket}"
            if ready.wait(timeout=5)
            else f"broker: not yet listening on {args.broker_socket}"
        )

    if not args.once:
        _write_info()
    lan_service = None if args.once else lan.start_service(_log)
    wan_service = None if args.once else wan.start_service(_log)

    workers: list[threading.Thread] = []
    last_reap = 0.0
    last_monitor = 0.0
    last_record = 0.0
    last_ci = 0.0
    ci_every = 0.0 if args.once else config.ci_poll()
    ci_thread: threading.Thread | None = None
    try:
        while True:
            if stop is not None and stop.is_set():
                return 0
            # Intents in different lanes (a push per project) run in parallel;
            # take_next_intent never hands out a second one from a busy lane.
            workers = [t for t in workers if t.is_alive()]
            if len(workers) < args.workers:
                intent = store.take_next_intent(conn, worker)
                if intent is not None:
                    t = threading.Thread(target=_handle_on_thread, args=(intent, ctx), daemon=True)
                    t.start()
                    workers.append(t)
                    continue
            if notifying:
                for m in _pending_for_human(conn, last_msg, everything=args.notify_all):
                    last_msg = m["id"]
                    if desktop.notify_message(m):
                        _log(f"🔔 {m['from_display']}: {str(m['body'])[:60]!r}")
            now = time.monotonic()
            if now - last_reap >= REAP_SESSIONS_EVERY:
                last_reap = now
                stale = store.reap_stale_intents(conn, STALE_AFTER)
                if stale:
                    _log(f"failed {stale} intent(s) stuck in 'running' past {STALE_AFTER}s")
                dead = store.reap_dead_sessions(conn)
                if dead:
                    _log(f"reaped {dead} session(s) whose claude process is gone")
            # Sample the machine and tell the fleet what changed. Reaping runs
            # first on purpose: a session ended a moment ago is what turns its
            # leftover processes into orphans.
            if not args.no_monitor and now - last_monitor >= args.monitor_every:
                last_monitor = now
                try:
                    for line in monitor.sweep(conn, thresholds, source=f"daemon:{worker}"):
                        _log(line)
                except Exception as e:  # a bad /proc read must not kill the daemon
                    _log(f"monitor: {type(e).__name__}: {e}")
            if ctx.save_messages and now - last_record >= RECORD_MESSAGES_EVERY:
                last_record = now
                record.ingest(conn, log=_log)
            # GitLab polls run on a thread with their own connection: a slow API
            # must not hold up the intent queue. One poll at a time.
            if ci_every and now - last_ci >= ci_every and not (ci_thread and ci_thread.is_alive()):
                last_ci = now
                ci_thread = threading.Thread(target=_ci_poll_thread, daemon=True)
                ci_thread.start()
            if args.once:
                if workers:
                    for t in workers:
                        t.join()
                    continue
                _log("queue empty — exiting")
                return 0
            store.touch(conn, DAEMON_SESSION)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _log("bye")
        return 0
    finally:
        # Let running actions finish, then fail what is still running: a row
        # left 'running' blocks its lane until a reaper sees it.
        deadline = time.monotonic() + WORKER_JOIN_TIMEOUT
        for t in workers:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in workers):
            n = store.reap_stale_intents(conn, 0, worker=worker)
            if n:
                _log(f"failed {n} intent(s) still running at exit")
        if lan_service is not None:
            lan_service.close()
        if wan_service is not None:
            wan_service.close()
        broker_stop.set()
        if broker_thread is not None:
            broker_thread.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
