"""Human console for the cabildo bus: watch what the agents say, and say
things back deterministically.

The MCP tools only exist inside an agent's session, which makes the human a
second-class citizen on his own bus: to post you had to ask an agent to post for
you, and to read you had to ask an agent to read. This module talks to the same
SQLite file directly, so it works from any terminal, needs nothing running, and
is unaffected by whether any agent happens to be awake.

    python -m cabildo.cli watch                 # full-screen chatroom (on a tty)
    python -m cabildo.cli watch --no-input      # plain tail, as before
    python -m cabildo.cli watch -t proj:paas    # talk to one channel
    python -m cabildo.cli say "ship it"         # post to #general as @jorge
    python -m cabildo.cli say "stop" -u         # urgent: agents can't idle on it
    python -m cabildo.cli say "hi" --to uptime  # direct message
    python -m cabildo.cli agents                # who's on the bus
    python -m cabildo.cli log -n 30             # recent history, then exit
    python -m cabildo.cli spawn qa "audit the gate" -r ~/repo   # queue an agent
    python -m cabildo.cli fleet                 # spawn ledger: who begat whom
    python -m cabildo.cli cost -d 7 --rate 3    # what running cabildo cost you

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from . import store
from .store import HUMAN_HANDLE, HUMAN_SESSION


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
class C:
    """ANSI colors, disabled when not a tty or when NO_COLOR is set."""

    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    DIM = "\033[2m" if on else ""
    BOLD = "\033[1m" if on else ""
    RED = "\033[31m" if on else ""
    GREEN = "\033[32m" if on else ""
    YELLOW = "\033[33m" if on else ""
    BLUE = "\033[34m" if on else ""
    MAGENTA = "\033[35m" if on else ""
    CYAN = "\033[36m" if on else ""
    OFF = "\033[0m" if on else ""


_SENDER_COLORS = (C.CYAN, C.GREEN, C.MAGENTA, C.BLUE, C.YELLOW)


def _sender_color(name: str) -> str:
    """Stable per-sender color so you can track one agent down the scroll."""
    return _SENDER_COLORS[sum(name.encode()) % len(_SENDER_COLORS)]


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _who(conn, session_id: str) -> str:
    """@handle when the session has one, else its short id."""
    return store.display_name(store.get_session(conn, session_id) if conn else None, session_id)


def render(m: dict, *, conn=None, body_indent: str = "    ") -> str:
    who = m["from_display"]
    dest = _who(conn, m["to_session"]) if m["to_session"] else f"#{m['topic']}"
    urgent = f" {C.RED}{C.BOLD}⚠ URGENT{C.OFF}" if m["urgent"] else ""
    col = _sender_color(who)
    head = (
        f"{C.DIM}{_clock(m['created_at'])}{C.OFF} "
        f"{col}{C.BOLD}{who}{C.OFF} {C.DIM}→{C.OFF} {dest}{urgent}"
    )
    body = "\n".join(body_indent + ln for ln in m["body"].splitlines())
    return f"{head}\n{body}"


# --------------------------------------------------------------------------- #
# Human identity
# --------------------------------------------------------------------------- #
def ensure_human(conn) -> None:
    """Register the human as a session so agents can DM him and see him listed.
    project_name is left unset: he is not scoped to one repo."""
    store.register_session(conn, HUMAN_SESSION, cwd=os.path.expanduser("~"))
    store.set_handle(conn, HUMAN_SESSION, HUMAN_HANDLE)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_say(args) -> int:
    body = " ".join(args.text).strip()
    if not body:
        print("nothing to say", file=sys.stderr)
        return 1
    conn = store.connect()
    ensure_human(conn)

    to_session = None
    topic = None
    if args.to:
        to_session = _resolve(conn, args.to)
        if to_session is None:
            print(f"no agent matches '{args.to}' — try `agents`", file=sys.stderr)
            return 1
    else:
        topic = (args.topic or store.DEFAULT_TOPIC).lstrip("#")
        store.ensure_topic(conn, topic, created_by=HUMAN_SESSION)

    mid = store.post_message(
        conn,
        from_session=HUMAN_SESSION,
        from_handle=HUMAN_HANDLE,
        body=body,
        topic=topic,
        to_session=to_session,
        urgent=args.urgent,
    )
    dest = _who(conn, to_session) if to_session else f"#{topic}"
    note = ""
    if args.urgent:
        note = (
            f"\n{C.DIM}urgent: agents with the Stop hook loaded cannot end their turn "
            f"until they read it. Sessions started before the hook, or idle ones, "
            f"still need a nudge (see `/loop`).{C.OFF}"
        )
    print(f"{C.GREEN}sent{C.OFF} #{mid} → {dest}{note}")
    return 0


def _resolve(conn, who: str) -> str | None:
    """Same generosity as the MCP server: exact id, handle, or short prefix."""
    who = who.lstrip("@")
    if store.get_session(conn, who):
        return who
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE handle=? ORDER BY last_seen_at DESC LIMIT 1",
        (who,),
    ).fetchone()
    if row:
        return row["session_id"]
    if len(who) >= 4:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ? "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (who.replace("%", "").replace("_", "") + "%",),
        ).fetchone()
        if row:
            return row["session_id"]
    return None


def _select(conn, topic: str | None, after_id: int, limit: int) -> list[dict]:
    q = "SELECT * FROM messages WHERE id > ?"
    a: list = [after_id]
    if topic:
        q += " AND topic = ?"
        a.append(topic.lstrip("#"))
    q += " ORDER BY id DESC LIMIT ?"
    a.append(limit)
    rows = list(conn.execute(q, a))
    return [store._msg_dict(r) for r in reversed(rows)]


def cmd_watch(args) -> int:
    # On a real terminal, watch is a chatroom: the human reads AND talks from
    # the same screen. Piped (or --no-input), it stays a plain greppable tail.
    if sys.stdin.isatty() and sys.stdout.isatty() and not args.no_input:
        from . import tui
        return tui.main(topic=args.topic, tail=args.tail)
    conn = store.connect()
    topic = args.topic
    scope = f"#{topic.lstrip('#')}" if topic else "all channels"
    print(f"{C.DIM}watching {scope} — ctrl-c to stop{C.OFF}\n")

    backlog = _select(conn, topic, 0, args.tail)
    for m in backlog:
        # flush: stdout is block-buffered when piped, and a tail that only
        # appears once you kill it is useless.
        print(render(m, conn=conn), "\n", flush=True)
    last = backlog[-1]["id"] if backlog else 0
    if not backlog:
        # Don't replay the whole history the first time something arrives.
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM messages").fetchone()
        last = row["m"]

    try:
        while True:
            for m in _select(conn, topic, last, 100):
                print(render(m, conn=conn), "\n", flush=True)
                last = m["id"]
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"{C.DIM}bye{C.OFF}")
        return 0


def cmd_log(args) -> int:
    conn = store.connect()
    msgs = _select(conn, args.topic, 0, args.n)
    if not msgs:
        print(f"{C.DIM}no messages{C.OFF}")
        return 0
    for m in msgs:
        print(render(m, conn=conn), "\n")
    return 0


def cmd_agents(args) -> int:
    conn = store.connect()
    rows = store.active_sessions(conn)
    now = time.time()
    print(
        f"{C.DIM}active = seen in the last {store.ACTIVE_WINDOW // 60}min. "
        f"This tracks last PROMPT, not liveness: an agent working a long "
        f"autonomous task, or idling with its window open, is missing from "
        f"this list.{C.OFF}\n"
    )
    if not rows:
        print("nobody on the bus")
        return 0
    for r in rows:
        name = store.display_name(r)
        ago = int(now - r["last_seen_at"])
        col = _sender_color(name)
        print(
            f"  {col}{C.BOLD}{name:<12}{C.OFF} {(r['project_name'] or '-'):<16} "
            f"{C.DIM}{r['cwd'] or ''} · last prompt {ago}s ago{C.OFF}"
        )
    claims = store.list_claims(conn)
    if claims:
        print(f"\n{C.BOLD}held scopes{C.OFF}")
        for c in claims:
            print(f"  {c['resource']:<28} {c['holder']:<12} {C.DIM}{c['note'] or ''}{C.OFF}")
    return 0


def cmd_spawn(args) -> int:
    """Queue a spawn intent as @jorge. The daemon executes it — this makes the
    cabildo the entrypoint: you never run `claude -p` by hand for fleet
    work, so every agent is born inside the caps, the budget, and the ledger."""
    from . import actions

    if args.role not in actions.ROLES:
        print(f"unknown role {args.role!r}; allowed: {sorted(actions.ROLES)}",
              file=sys.stderr)
        return 1
    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        print(f"not a directory: {repo}", file=sys.stderr)
        return 1
    conn = store.connect()
    ensure_human(conn)
    params = {
        "role": args.role, "task": args.task, "repo": repo, "target": args.target,
        "_from_session": HUMAN_SESSION, "_from_handle": HUMAN_HANDLE,
        "_project": repo,
    }
    if args.model:
        params["model"] = args.model
    iid = store.request_intent(
        conn, from_session=HUMAN_SESSION, from_handle=HUMAN_HANDLE,
        project=repo, action="spawn", params=params,
    )
    print(f"{C.GREEN}queued{C.OFF} intent #{iid}: spawn {args.role} "
          f"({args.model or 'default model'}) on {repo}")
    print(f"{C.DIM}a daemon with --allow-spawn must be running to execute it; "
          f"its result lands here as a DM (see `watch` / `fleet`).{C.OFF}")
    return 0


def _fmt_tokens(chars: int) -> str:
    from .transcript import CHARS_PER_TOKEN
    return f"{chars // CHARS_PER_TOKEN:,} tok"


def cmd_cost(args) -> int:
    """What running cabildo costs: context it injects (hook_metrics), the hook
    latency per tool call, and the tokens agents spend calling its tools
    (scanned from Claude Code transcripts)."""
    from . import transcript
    from .transcript import CHARS_PER_TOKEN

    since = time.time() - args.days * 86400 if args.days else None
    window = f"last {args.days}d" if args.days else "all time"
    conn = store.connect()

    # --- 1 & 2: passive overhead cabildo pushes into sessions + hook latency ---
    m = store.hook_metrics_summary(conn, since=since, session_id=args.session)
    t = m["total"]
    print(f"{C.BOLD}cabildo cost · {window}"
          f"{' · session ' + args.session[:8] if args.session else ''}{C.OFF}\n")

    print(f"{C.BOLD}1. Context injected by hooks{C.OFF} "
          f"{C.DIM}(banners, unread lines — billed every turn while in context){C.OFF}")
    if t["fires"]:
        print(f"   {t['chars']:,} chars ≈ {_fmt_tokens(t['chars'])} "
              f"over {t['fires']:,} hook fires, {t['sessions']} session(s)")
        print(f"\n{C.BOLD}2. Hook latency{C.OFF} "
              f"{C.DIM}(a subprocess per hook fire; the recurring cost when quiet){C.OFF}")
        print(f"   {'event':<16}{'fires':>8}{'injected':>12}{'chars':>10}{'avg ms':>9}{'total s':>10}")
        for e in m["per_event"]:
            print(f"   {e['event']:<16}{e['fires']:>8,}{e['injections']:>12,}"
                  f"{e['chars']:>10,}{(e['avg_ms'] or 0):>9.1f}{(e['total_ms'] or 0)/1000:>10.1f}")
    else:
        print(f"   {C.DIM}no hook_metrics yet — the instrumented hooks must be "
              f"installed & a session run before data appears.{C.OFF}")

    # --- 3: tokens agents spend calling cabildo tools (from transcripts) ---
    print(f"\n{C.BOLD}3. Agent tool traffic{C.OFF} "
          f"{C.DIM}(cabildo tool calls + results, scanned from transcripts — approx){C.OFF}")
    tr = transcript.scan(session_id=args.session)
    if tr["calls"]:
        print(f"   {tr['calls']:,} calls across {tr['files']} transcript(s): "
              f"{tr['input_chars']:,} in + {tr['output_chars']:,} out chars "
              f"≈ {tr['tokens_est']:,} tok")
        print(f"\n   {'tool':<28}{'calls':>8}{'in tok':>10}{'out tok':>10}")
        for name, e in sorted(tr["by_tool"].items(),
                              key=lambda kv: -(kv[1]["input_chars"] + kv[1]["output_chars"])):
            short = name.replace("mcp__cabildo__", "")
            print(f"   {short:<28}{e['calls']:>8,}"
                  f"{e['input_chars']//CHARS_PER_TOKEN:>10,}"
                  f"{e['output_chars']//CHARS_PER_TOKEN:>10,}")
    else:
        print(f"   {C.DIM}no cabildo tool calls found in transcripts.{C.OFF}")

    if args.rate:
        total_tok = t["chars"] // CHARS_PER_TOKEN + tr["tokens_est"]
        print(f"\n{C.BOLD}≈ €{total_tok / 1_000_000 * args.rate:.4f}{C.OFF} "
              f"{C.DIM}at €{args.rate}/1M tok (rough; ignores prompt caching, "
              f"which makes re-sent context much cheaper){C.OFF}")
    else:
        print(f"\n{C.DIM}Pass --rate <€ per 1M tokens> for a € estimate. Caveat: "
              f"prompt caching makes re-sent context far cheaper than raw tokens.{C.OFF}")
    return 0


def cmd_fleet(args) -> int:
    conn = store.connect()
    rows = store.list_spawns(conn, limit=args.n)
    if not rows:
        print(f"{C.DIM}no spawned agents on record{C.OFF}")
        return 0
    icons = {
        "launched": ("○", C.YELLOW),    # process up, not yet on the bus
        "registered": ("●", C.GREEN),   # alive and talking
        "ended": ("✓", C.DIM),          # finished cleanly
        "dead": ("✗", C.RED),           # died without ending its session
    }
    now = time.time()
    for r in rows:
        icon, col = icons.get(r["status"], ("?", C.DIM))
        name = r["agent_display"] or r["tag"]
        age = int(now - r["created_at"])
        age_s = f"{age // 3600}h{(age % 3600) // 60:02d}m" if age >= 3600 else f"{age // 60}m"
        print(f"  {col}{icon}{C.OFF} {C.BOLD}{name:<16}{C.OFF} {r['role']:<4} "
              f"depth {r['depth']}  by {r['parent_display']:<10} {age_s:>6} ago  "
              f"{C.DIM}{r['model'] or 'default'} · {r['status']}{C.OFF}")
        first_line = (r["task"] or "").strip().splitlines()[0]
        print(f"      {C.DIM}{first_line[:110]}{C.OFF}")
        if r["worktree"]:
            print(f"      {C.DIM}{r['worktree']}{C.OFF}")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cabildo", description=(__doc__ or "").split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="chatroom on a tty; plain tail when piped")
    w.add_argument("-t", "--topic", help="channel your messages go to (tail: filter)")
    w.add_argument("-n", "--tail", type=int, default=50, help="backlog lines first")
    w.add_argument("--interval", type=float, default=1.0, help="poll seconds (tail mode)")
    w.add_argument("--no-input", action="store_true", help="plain tail even on a tty")
    w.set_defaults(fn=cmd_watch)

    s = sub.add_parser("say", help="post to the bus as @jorge")
    s.add_argument("text", nargs="+")
    s.add_argument("-t", "--topic", help="channel (default: general)")
    s.add_argument("--to", help="direct message: handle, id, or short id")
    s.add_argument("-u", "--urgent", action="store_true", help="blocks the recipient from idling")
    s.set_defaults(fn=cmd_say)

    lg = sub.add_parser("log", help="print recent messages and exit")
    lg.add_argument("-n", type=int, default=20)
    lg.add_argument("-t", "--topic")
    lg.set_defaults(fn=cmd_log)

    a = sub.add_parser("agents", help="who is on the bus")
    a.set_defaults(fn=cmd_agents)

    sp = sub.add_parser("spawn", help="queue a headless agent in its own worktree")
    sp.add_argument("role", help="pm | qa | ux")
    sp.add_argument("task", help="the instruction (quote it)")
    sp.add_argument("-r", "--repo", default=".", help="repo path (default: cwd)")
    sp.add_argument("--target", default="main", help="branch the work lands on")
    sp.add_argument("-m", "--model", choices=["haiku", "sonnet", "opus"],
                    help="haiku/sonnet stretch the shared usage window")
    sp.set_defaults(fn=cmd_spawn)

    fl = sub.add_parser("fleet", help="spawn ledger: who begat whom, and their fate")
    fl.add_argument("-n", type=int, default=20)
    fl.set_defaults(fn=cmd_fleet)

    co = sub.add_parser("cost", help="what running cabildo costs: injected context, hook latency, tool traffic")
    co.add_argument("-d", "--days", type=float, default=0,
                    help="only the last N days (default: all time)")
    co.add_argument("--session", help="scope to one session id")
    co.add_argument("--rate", type=float,
                    help="€ per 1M tokens, for a rough € estimate")
    co.set_defaults(fn=cmd_cost)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
