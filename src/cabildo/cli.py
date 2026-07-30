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
    python -m cabildo.cli sessions              # your Claude sessions here, to resume a lost one
    python -m cabildo.cli log -n 30             # recent history, then exit
    python -m cabildo.cli spawn qa "audit the gate" -r ~/repo   # queue an agent
    python -m cabildo.cli fleet                 # spawn ledger: who begat whom
    python -m cabildo.cli cost -d 7 --rate 3    # what running cabildo cost you
    python -m cabildo.cli sync                   # push your sessions to the hosted dashboard
    python -m cabildo.cli add -p paas .          # put this folder on the bus (name + .mcp.json)
    python -m cabildo.cli service install        # run the daemon as a systemd --user service

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

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
    """Same generosity as the MCP server: exact id, handle, robot name, or short prefix."""
    who = who.lstrip("@")
    if store.get_session(conn, who):
        return who
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE handle=? ORDER BY last_seen_at DESC LIMIT 1",
        (who,),
    ).fetchone()
    if row:
        return row["session_id"]
    by_name = store.session_for_robot_name(conn, who)
    if by_name:
        return by_name
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


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _session_state(row, now: float) -> tuple[str, str, str]:
    """(icon, color, label) for a transcript's cabildo status. A transcript with
    no bus row is an older/untracked session — still resumable, just unlabelled
    by cabildo. `active` + recently seen is live; `active` + stale is idle
    (its claude is likely gone but no SessionEnd/reap fired yet); anything the
    bus closed reads as ended."""
    if row is None:
        return ("○", C.DIM, "no bus record")
    if row["status"] == "active" and (now - row["last_seen_at"]) <= store.ACTIVE_WINDOW:
        return ("●", C.GREEN, "live")
    if row["status"] == "active":
        return ("◐", C.YELLOW, "idle")
    return ("✓", C.DIM, "ended")


def cmd_sessions(args) -> int:
    """The recovery board: every Claude session in this project, newest first,
    each with the title you gave it (your first message), where you left off
    (your last), and the exact command to resume it. Joins Claude Code's own
    transcripts against cabildo's live/ended status so you can tell a running
    tab from one your laptop took down."""
    from . import transcript
    from .context import git_project

    conn = store.connect()
    if args.all:
        slug = None
        scope = "all projects"
    else:
        root, name = git_project(os.getcwd())
        slug = transcript.project_slug(root)
        scope = name
    digs = transcript.list_sessions(slug=slug)
    if args.limit:
        digs = digs[: args.limit]
    if not digs:
        print(f"{C.DIM}no transcripts found for {scope}{C.OFF}")
        return 0

    now = time.time()
    print(
        f"{C.DIM}sessions for {C.OFF}{C.BOLD}{scope}{C.OFF}{C.DIM} — newest first. "
        f"resume any with `claude --resume <id>`.{C.OFF}\n"
    )
    for d in digs:
        sid = d["session_id"]
        row = store.get_session(conn, sid)
        name = store.display_name(row, sid)
        icon, col, state = _session_state(row, now)
        turns = d["turns"]
        plural = "s" if turns != 1 else ""
        namecol = _sender_color(name)
        print(
            f"  {col}{icon}{C.OFF} {namecol}{C.BOLD}{name:<24}{C.OFF} "
            f"{C.DIM}{_ago(now - d['mtime']):>4} ago · {turns} turn{plural} · {state}{C.OFF}"
        )
        print(f"      {d['title'][:100]}")
        last = (d.get("last") or "").strip()
        if last and last != d["title"]:
            print(f"      {C.DIM}↳ left off: {last[:88]}{C.OFF}")
        print(f"      {C.DIM}claude --resume {sid}{C.OFF}")
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
# Cloud mirror
# --------------------------------------------------------------------------- #
def cmd_sync(args) -> int:
    """Push this laptop's bus up to the hosted dashboard, and pull back anything
    typed into it. Observability flows up; only human replies flow down."""
    from . import cloud

    if args.set_token:
        p = cloud.save_token(args.set_token, args.base)
        print(f"{C.GREEN}saved{C.OFF} cloud token → {p}")
        return 0
    try:
        base, token = cloud.resolve(base=args.base)
    except RuntimeError as e:
        print(f"{C.DIM}{e}{C.OFF}", file=sys.stderr)
        return 2

    def once() -> None:
        r = cloud.sync_once(base, token)
        if not r.get("ok"):
            print(f"{C.DIM}sync failed: {r.get('error')}{C.OFF}", file=sys.stderr)
            return
        d = cloud.drain_once(base, token)
        stamp = datetime.now().strftime("%H:%M:%S")
        cost = f" · {C.DIM}+cost{C.OFF}" if r.get("cost") else ""
        down = f" · {C.GREEN}{d['delivered']} in{C.OFF}" if d.get("delivered") else ""
        print(f"{C.DIM}{stamp}{C.OFF} {C.GREEN}synced{C.OFF} "
              f"{r['agents']} agents · {r['messages']} new msgs · {r['claims']} locks{cost} "
              f"→ {base}{down}")

    if not args.loop:
        once()
        return 0
    print(f"{C.DIM}mirroring {store.db_path()} → {base} every {args.interval}s "
          f"(Ctrl-C to stop){C.OFF}")
    try:
        while True:
            once()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{C.DIM}stopped{C.OFF}")
    return 0


def cmd_share(args) -> int:
    """Manage which projects may push their conversation content (your prompts +
    Claude's replies) to the hosted dashboard. Opt-in per project: everything
    NOT listed here stays on this laptop. Personal 'second-brain' sessions must
    never be added."""
    from . import cloud

    if args.project:
        project = args.project.strip().lower()
        if args.remove:
            now = cloud.remove_shared_project(project)
            print(f"{C.YELLOW}unshared{C.OFF} {project}")
        else:
            now = cloud.add_shared_project(project)
            print(f"{C.GREEN}shared{C.OFF} {project} — its prompts + replies will "
                  f"now mirror to the dashboard")
    else:
        now = cloud.shared_projects()

    if now:
        print(f"\n{C.BOLD}shared projects{C.OFF} "
              f"{C.DIM}(conversation content pushed to the cloud):{C.OFF}")
        for p in now:
            print(f"  {C.CYAN}{p}{C.OFF}")
    else:
        print(f"{C.DIM}no projects shared — all conversation content stays local.{C.OFF}")
    return 0


def _known_projects() -> list[str]:
    """Every project name the local bus has seen — the first-class list of
    'folders' the user coordinates in — best-effort, read-only."""
    from . import store
    try:
        import sqlite3
        dbp = store.db_path()
        if not dbp.exists():
            return []
        conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT DISTINCT project_name FROM sessions "
                "WHERE project_name IS NOT NULL AND project_name<>''").fetchall()
        finally:
            conn.close()
        return sorted({str(r[0]).strip().lower() for r in rows if r[0]})
    except Exception:
        return []


# what each rung of the uplink ladder lets leave the machine, for the CLI legend
_UPLINK_BLURB = {
    "none": "invisible — never reaches the cloud",
    "presence": "agents + locks show up; no words leave",
    "messages": "+ inter-agent bus messages",
    "content": "+ full conversation (your prompts & Claude's replies)",
}


def cmd_project(args) -> int:
    """Projects are folders on this machine, each isolated by default. Two dials:
    --uplink controls how much reaches the hosted cloud (none < presence <
    messages < content); --link/--unlink control which other projects it may see
    on the local bus. With no args, lists every known project and its dials."""
    from . import cloud

    if args.default is not None:
        lvl = cloud.set_default_uplink(args.default)
        print(f"{C.GREEN}default uplink{C.OFF} → {C.CYAN}{lvl}{C.OFF} "
              f"{C.DIM}({_UPLINK_BLURB.get(lvl, '')}){C.OFF}")
        if not args.project:
            return 0

    if args.project:
        name = args.project.strip().lower()
        if args.uplink is not None:
            try:
                lvl = cloud.set_uplink(name, args.uplink)
            except ValueError as e:
                print(f"{C.RED}{e}{C.OFF}")
                return 2
            print(f"{C.GREEN}{name}{C.OFF} uplink → {C.CYAN}{lvl}{C.OFF} "
                  f"{C.DIM}({_UPLINK_BLURB.get(lvl, '')}){C.OFF}")
        if args.link:
            cloud.add_peer(name, args.link.strip().lower())
            print(f"{C.GREEN}linked{C.OFF} {name} ↔ {args.link.strip().lower()} "
                  f"{C.DIM}(they can see each other on the bus){C.OFF}")
        if args.unlink:
            cloud.remove_peer(name, args.unlink.strip().lower())
            print(f"{C.YELLOW}unlinked{C.OFF} {name} ✗ {args.unlink.strip().lower()}")
        # Always show the project's resulting state.
        eff = cloud.uplink_of(name)
        peers = cloud.peers_of(name)
        listed = name in cloud.project_registry()
        tag = "" if listed else f" {C.DIM}(default){C.OFF}"
        print(f"\n{C.BOLD}{name}{C.OFF}")
        print(f"  uplink  {C.CYAN}{eff}{C.OFF}{tag} "
              f"{C.DIM}— {_UPLINK_BLURB.get(eff, '')}{C.OFF}")
        print(f"  peers   {C.CYAN}{', '.join(peers) if peers else '—'}{C.OFF} "
              f"{C.DIM}(local bus visibility){C.OFF}")
        roots = cloud.roots_of(name)
        print(f"  folders {C.CYAN}{', '.join(roots) if roots else '—'}{C.OFF} "
              f"{C.DIM}(pinned with `cabildo add`){C.OFF}")
        return 0

    # No project → the whole board.
    reg = cloud.project_registry()
    default = cloud.default_uplink()
    names = sorted(set(reg) | set(_known_projects()))
    print(f"{C.BOLD}projects{C.OFF}  "
          f"{C.DIM}default uplink: {C.OFF}{C.CYAN}{default}{C.OFF}")
    print(f"{C.DIM}uplink ladder: none < presence < messages < content{C.OFF}\n")
    if not names:
        print(f"{C.DIM}no projects seen yet.{C.OFF}")
        return 0
    for n in names:
        eff = cloud.uplink_of(n)
        peers = cloud.peers_of(n)
        listed = n in reg
        col = C.RED if eff == "none" else (C.GREEN if eff == "content" else C.CYAN)
        dial = f"{col}{eff}{C.OFF}" + ("" if listed else f"{C.DIM}*{C.OFF}")
        line = f"  {C.BOLD}{n:<20}{C.OFF} {dial}"
        if peers:
            line += f"   {C.DIM}↔ {', '.join(peers)}{C.OFF}"
        for root in cloud.roots_of(n):
            line += f"\n  {C.DIM}{'':<20} ↳ {root}{C.OFF}"
        print(line)
    print(f"\n{C.DIM}* = using the default; set with "
          f"`cabildo project <name> --uplink <level>`{C.OFF}")
    return 0


def _hooks_installed() -> bool:
    """Whether ~/.claude/settings.json runs cabildo-hook. The MCP server alone
    gives an agent the tools; the hooks are what make it join, be seen, and be
    held to its claims — so a folder wired with only half of that is worth
    saying out loud."""
    try:
        text = Path(os.path.expanduser("~/.claude/settings.json")).read_text()
    except OSError:
        return False
    return "cabildo-hook" in text


def cmd_add(args) -> int:
    """Put a folder on the bus: pin its project name for this machine and wire
    its .mcp.json, so every Claude Code session opened there joins cabildo
    without anyone remembering to configure anything."""
    from . import cloud, mcp_config
    from .context import _git_project

    target = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(target):
        print(f"{C.RED}not a directory:{C.OFF} {target}", file=sys.stderr)
        return 2

    git_root, git_name = _git_project(Path(target))
    # Projects are repos: .mcp.json is only read from the directory Claude Code
    # is launched in, so burying it in a subfolder would wire nothing. --exact
    # is the escape hatch for one package of a monorepo being its own project.
    root = target if args.exact else git_root
    name = (args.project or os.path.basename(root)).strip().lower()

    if args.remove:
        owner = cloud.unregister_root(root)
        try:
            p, what = mcp_config.remove(root)
        except (ValueError, OSError) as e:
            p, what = mcp_config.config_path(root), f"left alone ({e})"
        print(f"{C.YELLOW}unregistered{C.OFF} {root}"
              + (f" {C.DIM}(was {owner}){C.OFF}" if owner else f" {C.DIM}(was not registered){C.OFF}"))
        print(f"  {C.DIM}{p} — {what}{C.OFF}")
        return 0

    if args.print_only:
        print(f"{C.DIM}would register{C.OFF} {C.BOLD}{name}{C.OFF} → {root}")
        print(f"{C.DIM}would write {mcp_config.config_path(root)}:{C.OFF}")
        print(mcp_config.preview(root, name))
        return 0

    try:
        name, stored = cloud.register_root(name, root, uplink=args.uplink)
    except ValueError as e:
        print(f"{C.RED}{e}{C.OFF}", file=sys.stderr)
        return 2
    if args.link:
        cloud.add_peer(name, args.link.strip().lower())

    print(f"{C.GREEN}added{C.OFF} {C.BOLD}{name}{C.OFF} {C.DIM}→{C.OFF} {stored}")
    if name != git_name and not args.exact:
        print(f"  {C.DIM}git would have called this {git_name!r}; pinned to "
              f"{name!r} for every session here{C.OFF}")
    if root != target:
        print(f"  {C.DIM}resolved up to the repo root (use --exact to register "
              f"{target} itself){C.OFF}")

    if not args.no_mcp:
        try:
            p, what = mcp_config.apply(root, name)
        except (ValueError, OSError) as e:
            print(f"  {C.RED}could not write .mcp.json: {e}{C.OFF}", file=sys.stderr)
            return 1
        cmd = " ".join(mcp_config.mcp_command())
        color = C.DIM if what == "unchanged" else C.GREEN
        print(f"  {color}{what}{C.OFF} {p} {C.DIM}({cmd}){C.OFF}")
        print(f"  {C.DIM}Claude Code asks once to approve a project MCP server; "
              f".mcp.json is meant to be committed — gitignore it if this wiring "
              f"is yours alone.{C.OFF}")

    eff = cloud.uplink_of(name)
    peers = cloud.peers_of(name)
    print(f"  {C.DIM}uplink{C.OFF} {C.CYAN}{eff}{C.OFF} "
          f"{C.DIM}— {_UPLINK_BLURB.get(eff, '')}{C.OFF}")
    if peers:
        print(f"  {C.DIM}peers{C.OFF}  {C.CYAN}{', '.join(peers)}{C.OFF}")
    if not _hooks_installed():
        print(f"\n{C.YELLOW}note{C.OFF} no cabildo hooks in ~/.claude/settings.json — "
              f"sessions here will have the tools but won't auto-join, be listed, "
              f"or have claims enforced. Run {C.BOLD}python scripts/install.py{C.OFF} once.")
    print(f"\n{C.DIM}restart Claude Code in that folder to pick it up.{C.OFF}")
    return 0


# --------------------------------------------------------------------------- #
# Background services
# --------------------------------------------------------------------------- #
def _service_units(args):
    """The units `install` should write: the daemon, plus the mirror if asked —
    narrowed to one when --unit names it, which is how you install the rest
    after refusing to overwrite a hand-rolled unit."""
    from . import service
    units = []
    if args.unit in (None, "daemon"):
        units.append(service.daemon_unit(
            allow_push=args.allow_push,
            allow_spawn=args.allow_spawn,
            max_agents=args.max_agents,
            max_depth=args.max_depth,
            budget=args.budget,
            budget_days=args.budget_days,
            max_intent_age=args.max_intent_age,
            notify=not args.no_notify,
        ))
    if args.sync or args.unit == "sync":
        units.append(service.sync_unit(interval=args.sync_interval, base=args.base))
    return units


def _service_selected(args):
    """Which units a status/logs/restart/uninstall applies to: what --unit says,
    else whatever is actually installed, else both shapes so status can report
    'not installed' rather than nothing."""
    from . import service
    units = [service.daemon_unit(), service.sync_unit()]
    if args.unit:
        units = [u for u in units if u.key == args.unit]
    installed = [u for u in units if u.path.exists()]
    return installed or units


def _print_steps(steps) -> int:
    rc = 0
    for label, code, out in steps:
        mark = f"{C.GREEN}✓{C.OFF}" if code == 0 else f"{C.RED}✗{C.OFF}"
        print(f"  {mark} {label}" + (f" {C.DIM}{out}{C.OFF}" if out else ""))
        rc = rc or code
    return rc


def cmd_service(args) -> int:
    """Install cabildo's long-lived pieces as systemd --user services, so the
    daemon (and optionally the cloud mirror) survive closing the terminal."""
    from . import service

    reason = service.unavailable_reason()
    # --print still works without systemd: on a machine that can't run these
    # units, the rendered ExecStart is exactly what you need to write your own.
    if reason and not args.print_only:
        print(f"{C.RED}systemd --user not usable here{C.OFF}\n  {reason}", file=sys.stderr)
        return 2

    if args.action == "install":
        units = _service_units(args)
        if args.print_only:
            for u in units:
                print(f"{C.DIM}# {u.path}{C.OFF}")
                print(service.render(u))
            return 0
        clash = [u for u in units if service.foreign(u)]
        if clash and not args.force:
            print(f"{C.RED}refusing to overwrite unit files cabildo didn't write:{C.OFF}",
                  file=sys.stderr)
            for u in clash:
                print(f"  {u.path}\n    {C.DIM}ExecStart={service.exec_start_of(u.path)}{C.OFF}",
                      file=sys.stderr)
            others = [u.key for u in units if u not in clash]
            print(f"\n{C.DIM}that one is doing a job this installer may not do the same "
                  f"way — read it first. Then either --force to replace it, or "
                  f"install just the rest: {C.OFF}"
                  + (f"cabildo service install --unit {others[0]} ..." if others
                     else "(nothing else to install)"), file=sys.stderr)
            return 2
        print(f"{C.BOLD}installing{C.OFF} {', '.join(u.name for u in units)} "
              f"{C.DIM}→ {service.unit_dir()}{C.OFF}")
        rc = _print_steps(service.install(units, start=not args.no_start,
                                          force=args.force))
        if args.linger:
            code, out = service.enable_linger()
            _print_steps([("enable-linger", code, out)])
        elif service.linger_enabled() is False:
            print(f"  {C.DIM}services stop when you log out; "
                  f"`cabildo service install --linger` keeps them running.{C.OFF}")
        daemon = next((u for u in units if u.key == service.DAEMON), None)
        if daemon and not args.allow_push and not args.allow_spawn:
            print(f"\n{C.YELLOW}dry-run daemon{C.OFF} {C.DIM}— it will report what it "
                  f"WOULD do and push/spawn nothing. Re-run with --allow-push "
                  f"--allow-spawn when you trust it.{C.OFF}")
        print(f"\n{C.DIM}logs: cabildo service logs -f{C.OFF}")
        return rc

    if args.action == "uninstall":
        units = _service_selected(args)
        print(f"{C.BOLD}removing{C.OFF} {', '.join(u.name for u in units)}")
        return _print_steps(service.uninstall(units, force=args.force))

    if args.action in ("start", "stop", "restart"):
        units = _service_selected(args)
        steps = []
        for u in units:
            p = service.systemctl(args.action, u.name)
            steps.append((f"{args.action} {u.name}", p.returncode,
                          ((p.stderr or "") + (p.stdout or "")).strip()))
        return _print_steps(steps)

    if args.action == "logs":
        units = _service_selected(args)
        cmd = ["journalctl", "--user", "-n", str(args.lines)]
        for u in units:
            cmd += ["-u", u.name]
        if args.follow:
            cmd.append("-f")
        import subprocess
        try:
            return subprocess.call(cmd)
        except FileNotFoundError:
            print(f"{C.RED}no journalctl on PATH{C.OFF}", file=sys.stderr)
            return 2

    # status (default)
    from . import service as sv
    print(f"{C.BOLD}cabildo services{C.OFF} {C.DIM}{sv.unit_dir()}{C.OFF}")
    any_installed = False
    for u in (sv.daemon_unit(), sv.sync_unit()):
        st = sv.status(u)
        if not st["installed"]:
            print(f"  {C.DIM}○ {u.name:<24} not installed{C.OFF}")
            continue
        any_installed = True
        active = st.get("ActiveState", "?")
        col = C.GREEN if active == "active" else (C.RED if active == "failed" else C.YELLOW)
        since = st.get("ExecMainStartTimestamp") or ""
        extra = f"restarts {st['NRestarts']}" if st.get("NRestarts") not in (None, "0") else ""
        print(f"  {col}●{C.OFF} {C.BOLD}{u.name:<24}{C.OFF} {col}{active}"
              f"{C.OFF}/{st.get('SubState', '?')} "
              f"{C.DIM}{st.get('UnitFileState', '?')}"
              + (f" · since {since}" if since else "")
              + (f" · {extra}" if extra else "") + f"{C.OFF}")
        if sv.foreign(u):
            print(f"      {C.YELLOW}not written by cabildo{C.OFF} "
                  f"{C.DIM}— {sv.exec_start_of(u.path)}{C.OFF}")
    if not any_installed:
        print(f"\n{C.DIM}nothing installed yet: "
              f"{C.OFF}cabildo service install --allow-push{C.DIM} "
              f"(add --sync to mirror to the dashboard){C.OFF}")
        return 0
    linger = sv.linger_enabled()
    if linger is False:
        print(f"  {C.DIM}linger off — these stop when you log out{C.OFF}")
    return 0


def cloud_uplink_levels() -> tuple:
    from . import cloud
    return cloud.UPLINK_LEVELS


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

    se = sub.add_parser(
        "sessions",
        help="your Claude sessions in this project, with titles, to find & resume a lost one",
    )
    se.add_argument("-a", "--all", action="store_true",
                    help="every project, not just this one")
    se.add_argument("-n", "--limit", type=int, default=20, help="max sessions to show")
    se.set_defaults(fn=cmd_sessions)

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

    sy = sub.add_parser("sync", help="mirror this laptop's bus up to the hosted dashboard")
    sy.add_argument("-l", "--loop", action="store_true", help="keep syncing (default: one pass)")
    sy.add_argument("--interval", type=float, default=60.0, help="loop seconds (default: 60)")
    sy.add_argument("--base", help="override the cloud URL")
    sy.add_argument("--set-token", metavar="TOKEN",
                    help="save a cloud token to ~/.cabildo/cloud.json and exit")
    sy.set_defaults(fn=cmd_sync)

    sh = sub.add_parser(
        "share",
        help="opt a project's conversation content into the dashboard (default: list)")
    sh.add_argument("project", nargs="?",
                    help="project to share; omit to list what's currently shared")
    sh.add_argument("--remove", action="store_true", help="unshare instead of share")
    sh.set_defaults(fn=cmd_share)

    pr = sub.add_parser(
        "project",
        help="list projects and set their dials: cloud uplink + local peer links")
    pr.add_argument("project", nargs="?", help="project to inspect or configure")
    pr.add_argument("--uplink", choices=cloud_uplink_levels(),
                    help="how much reaches the cloud (none<presence<messages<content)")
    pr.add_argument("--link", metavar="OTHER",
                    help="let this project and OTHER see each other on the bus")
    pr.add_argument("--unlink", metavar="OTHER", help="undo a --link")
    pr.add_argument("--default", choices=cloud_uplink_levels(),
                    help="set the default uplink for projects not listed")
    pr.set_defaults(fn=cmd_project)

    ad = sub.add_parser(
        "add",
        help="put a folder on the bus: pin its project name + wire its .mcp.json")
    ad.add_argument("path", nargs="?", default=".", help="folder (default: cwd)")
    ad.add_argument("-p", "--project", metavar="NAME",
                    help="project name to pin this folder to (default: repo dir name)")
    ad.add_argument("--uplink", choices=cloud_uplink_levels(),
                    help="how much of it reaches the cloud (default: your default_uplink)")
    ad.add_argument("--link", metavar="OTHER",
                    help="also let this project and OTHER see each other on the bus")
    ad.add_argument("--exact", action="store_true",
                    help="register the given folder itself, not the git repo containing it")
    ad.add_argument("--no-mcp", action="store_true",
                    help="pin the name only; don't touch .mcp.json")
    ad.add_argument("--remove", action="store_true",
                    help="undo: unpin the folder and drop cabildo from its .mcp.json")
    ad.add_argument("--print", dest="print_only", action="store_true",
                    help="show what would be written, change nothing")
    ad.set_defaults(fn=cmd_add)

    sv = sub.add_parser(
        "service",
        help="run the daemon (and cloud sync) as systemd --user services")
    sv.add_argument("action", nargs="?", default="status",
                    choices=["install", "status", "logs", "start", "stop",
                             "restart", "uninstall"],
                    help="default: status")
    sv.add_argument("--sync", action="store_true",
                    help="install: also run `cabildo sync --loop` as a service")
    sv.add_argument("--sync-interval", type=float, default=60.0,
                    help="seconds between cloud syncs (default: 60)")
    sv.add_argument("--base", help="override the cloud URL for the sync unit")
    sv.add_argument("--allow-push", action="store_true",
                    help="let the daemon actually push (default: dry run)")
    sv.add_argument("--allow-spawn", action="store_true",
                    help="let the daemon actually launch agents (default: dry run)")
    sv.add_argument("--max-agents", type=int, default=3, help="concurrent spawned agents")
    sv.add_argument("--max-depth", type=int, default=2, help="how deep spawn chains may go")
    sv.add_argument("--budget", type=float, default=0.0,
                    help="weighted-token ceiling over --budget-days (0 = no throttle)")
    sv.add_argument("--budget-days", type=float, default=7.0, help="window for --budget")
    sv.add_argument("--max-intent-age", type=float, default=2.0,
                    help="hours a queued intent may wait before the daemon refuses "
                         "it as stale instead of running it (0 disables)")
    sv.add_argument("--no-notify", action="store_true",
                    help="don't pop desktop notifications for messages addressed to you")
    sv.add_argument("--linger", action="store_true",
                    help="keep the services running after you log out (loginctl enable-linger)")
    sv.add_argument("--no-start", action="store_true",
                    help="install: enable at login but don't start now")
    sv.add_argument("--unit", choices=["daemon", "sync"],
                    help="restrict the action to one unit")
    sv.add_argument("--force", action="store_true",
                    help="replace (or remove) a unit file cabildo didn't write")
    sv.add_argument("-n", "--lines", type=int, default=40, help="logs: how many lines")
    sv.add_argument("-f", "--follow", action="store_true", help="logs: keep streaming")
    sv.add_argument("--print", dest="print_only", action="store_true",
                    help="install: print the unit files instead of writing them")
    sv.set_defaults(fn=cmd_service)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
