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
    python -m cabildo.cli say "api is green"     # post to #general as @jorge
    python -m cabildo.cli say "stop" -u         # urgent: agents can't idle on it
    python -m cabildo.cli say "hi" --to uptime  # direct message
    python -m cabildo.cli agents                # who's on the bus
    python -m cabildo.cli sessions              # your Claude sessions here, to resume a lost one
    python -m cabildo.cli log -n 30             # recent history, then exit
    python -m cabildo.cli spawn qa "audit the gate" -r ~/repo   # queue an agent
    python -m cabildo.cli fleet                 # spawn ledger: who begat whom
    python -m cabildo.cli mem                   # RAM, attributed to the agents holding it
    python -m cabildo.cli conditions            # what the fleet is being told right now
    python -m cabildo.cli alert "CI is down"    # nudge every session on this machine
    python -m cabildo.cli cost -d 7 --rate 3    # what running cabildo cost you
    python -m cabildo.cli add -p paas .          # put this folder on the bus (name + .mcp.json)
    python -m cabildo.cli viewer                  # read-only web view of the local bus on localhost
    python -m cabildo.cli doctor                   # verify the install + flag platform gaps
    python -m cabildo.cli service install        # run the daemon as a systemd --user service

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__, config, names, store
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
    dest = _who(conn, m["to_session"]) if m["to_session"] else store.dest_label(m)
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
    project_name is left unset: he is not scoped to one repo.
    """
    store.register_session(conn, HUMAN_SESSION, cwd=os.path.expanduser("~"))
    store.set_handle(conn, HUMAN_SESSION, HUMAN_HANDLE)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_pair(args) -> int:
    """Pair this machine with another on the LAN through the daemon, the same as the
    MCP ``request("pair")``. Without a PIN: print one to enter on the other machine.
    """
    params: dict[str, Any] = {}
    if args.pin:
        params["pin"] = args.pin
    if args.host:
        params |= {"host": args.host, "port": args.port}
    conn = store.connect()
    ensure_human(conn)
    iid = store.request_intent(
        conn,
        from_session=HUMAN_SESSION,
        from_handle=HUMAN_HANDLE,
        project=None,
        action="pair",
        params=params | {"_from_session": HUMAN_SESSION, "_from_handle": HUMAN_HANDLE},
    )
    end = time.time() + args.wait
    while time.time() < end:
        row = conn.execute("SELECT * FROM intents WHERE id=?", (iid,)).fetchone()
        d = store.intent_dict(row) if row else None
        if d and d["status"] not in ("pending", "running"):
            result = d.get("result") or {}
            print(result.get("reason") or result.get("error") or json.dumps(result))
            return 0 if d["status"] == "done" and result.get("ok", True) else 1
        time.sleep(0.5)
    print(f"no answer from the daemon in {args.wait:.0f}s (is it running? `cabildo doctor`)")
    return 1


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
    # An unset -t falls back to [watch].topic from config (e.g. "project", this
    # repo's own channel), else all channels.
    topic = args.topic or config.watch_topic()
    if getattr(args, "snapshot", False):
        from . import tui

        print(tui.snapshot(topic=topic, tail=args.tail, size=(args.rows, args.cols)))
        return 0
    if sys.stdin.isatty() and sys.stdout.isatty() and not args.no_input:
        from . import tui

        return tui.main(topic=topic, tail=args.tail)
    conn = store.connect()
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


# --------------------------------------------------------------------------- #
# Push delivery — a machine-readable feed for the bundled Claude Code mod
#
# The MCP tools (inbox/read/send/whoami) only exist inside a live agent turn.
# The push-delivery mod runs OUTSIDE a turn (a $.clock doorbell on an idle
# session), so it needs the same reads/writes as a plain subprocess with JSON
# out and message ids exposed. These four verbs are that surface; they reuse
# the store predicates so the inbox definition stays single-sourced.
# --------------------------------------------------------------------------- #
def _me_session(conn, args) -> str | None:
    """The session acting: an explicit --session wins (the mod learns its id
    once via `whoami` and passes it back, avoiding a pid walk per poll), else
    resolve it the way the git hook does (CABILDO_TAG / CABILDO_SESSION_ID /
    claude-pid ancestry).

    An explicit --session that names no registered session returns None so the
    caller can refuse, rather than silently acting as a ghost session.
    """
    sid = getattr(args, "session", None)
    if sid:
        return sid if store.get_session(conn, sid) is not None else None
    from . import guard

    return guard.resolve_actor(conn).session_id


def cmd_whoami(args) -> int:
    conn = store.connect()
    from . import guard

    sid = guard.resolve_actor(conn).session_id
    row = store.get_session(conn, sid)
    out = {
        "session_id": sid,
        "handle": row["handle"] if row else None,
        "role": row["role"] if row else None,
        "project_name": row["project_name"] if row else None,
        "is_human": sid == HUMAN_SESSION,
        # seed the poller's cursor to "now" so it wakes on what arrives after
        # it starts, not on the whole backlog.
        "cursor": store.max_message_id(conn),
    }
    if args.json:
        print(json.dumps(out))
    else:
        who = f"@{out['handle']}" if out["handle"] else sid
        print(f"{who}  {C.DIM}({sid}){C.OFF}  cursor={out['cursor']}")
    return 0


def _wakes(conn, sid: str, m: dict) -> bool:
    """Whether this message may start a turn in an idle session by default.

    Wakes: a DM the sender was allowed to send (same project, linked or granted
    project, shared chatroom, or a paired machine: the consent check is the
    opt-in); a role message; an urgent post in any room the session is in; any
    post in the session's own project channel; and a room post elsewhere that
    names the session (``@handle`` or robot name). A plain post in another room
    does not. The mod's token bucket still caps how often anything
    wakes it.
    """
    sender = m["from_session"]
    if m.get("auto"):
        return False  # a hook's notice ("subagent finished") is history, not a request
    if m["to_session"] == sid:
        row = store.get_session(conn, sender)
        if row is not None and str(row["client"] or "").startswith(store.REMOTE_PREFIX):
            return True  # the pairing is the consent
        return store.consent_error(conn, sender, to_session=sid) is None
    if m["to_role"]:
        return True  # addressed to a role this session holds
    if m["urgent"]:
        return True
    if _is_my_project_channel(conn, sid, str(m["topic"] or "")):
        return True  # project peers talking is the collaboration we want
    return _names_me(conn, sid, str(m["body"] or ""))


def _is_my_project_channel(conn, sid: str, topic: str) -> bool:
    """Whether ``topic`` is the ``proj:`` channel of this session's own project."""
    row = store.get_session(conn, sid)
    project = row["project_name"] if row is not None else None
    return bool(project) and topic == store.project_topic(project)


def _names_me(conn, sid: str, body: str) -> bool:
    """Whether ``body`` mentions this session as ``@handle`` or ``@robot-name``."""
    row = store.get_session(conn, sid)
    labels = [names.robot_name(sid)]
    if row is not None and row["handle"]:
        labels.append(str(row["handle"]))
    return any(
        re.search(rf"@{re.escape(n.lstrip('@'))}(?![\w-])", body, re.IGNORECASE) for n in labels
    )


def _pull_row(m: dict, preview_chars: int, *, wakes: bool = False) -> dict:
    """The projection the mod gates on. Bodies are previewed, never shipped
    whole: the mod decides whether to wake, then the model reads full bodies
    through the `inbox` tool inside the drain turn.
    """
    body = m["body"]
    preview = body[:preview_chars]
    return {
        "id": m["id"],
        "from_session": m["from_session"],
        "from_display": m["from_display"],
        # Robot name is derived deterministically from the session id, so it is
        # spoof-resistant (unlike a self-assigned handle) — the mod gates its
        # push allow-list on this, not on from_display.
        "from_robot": names.robot_name(m["from_session"]),
        "from_human": m["from_session"] == HUMAN_SESSION,
        "wakes": wakes,
        "to_session": m["to_session"],
        "to_role": m["to_role"],
        "reply_to": m["reply_to"],
        "topic": m["topic"],
        "urgent": m["urgent"],
        "read": m["read"],
        "created_at": m["created_at"],
        "preview": preview,
        "truncated": len(body) > len(preview),
    }


def _resolve_me(conn, args) -> str | None:
    """`_me_session` with the uniform 'unknown --session' error for a CLI verb."""
    sid = _me_session(conn, args)
    if sid is None:
        print(f"no such session: {args.session!r}", file=sys.stderr)
    return sid


def cmd_pull(args) -> int:
    conn = store.connect()
    sid = _resolve_me(conn, args)
    if sid is None:
        return 1
    msgs = store.inbox_after(
        conn,
        sid,
        after_id=args.after,
        urgent_only=args.urgent_only,
        limit=args.limit,
    )
    cursor = msgs[-1]["id"] if msgs else args.after
    if args.json:
        print(
            json.dumps(
                {
                    "session_id": sid,
                    "cursor": cursor,
                    "messages": [
                        _pull_row(m, args.preview_chars, wakes=_wakes(conn, sid, m)) for m in msgs
                    ],
                }
            )
        )
    else:
        if not msgs:
            print(f"{C.DIM}nothing after #{args.after}{C.OFF}")
        for m in msgs:
            print(render(m, conn=conn), "\n")
    return 0


# How often `wait` re-resolves the acting session, so a /clear (new session id,
# same pid) is reported to the mod instead of leaving it on the retired inbox.
WAIT_REBIND_S = 30.0


def cmd_wait(args) -> int:
    """Block until the inbox has a row with ``id > --after``, then print it in
    `pull`'s JSON shape and exit (#37).

    One process per wake instead of one ``pull`` spawn per second. It watches
    ``PRAGMA data_version`` (a cheap per-connection counter that moves when any
    other connection commits) and queries only when it moves. It exits 0 with
    no messages at ``--timeout``, so the caller respawns with a fresh binary;
    exits 0 silently when its parent dies, so a killed client leaves no orphan;
    and prints ``{"rebound": true, "session_id", "cursor"}`` when the pid walk
    resolves to a different session than ``--session`` (a /clear).
    """
    conn = store.connect()
    sid = _resolve_me(conn, args)
    if sid is None:
        return 1
    from . import guard

    parent = os.getppid()
    start = time.monotonic()
    next_rebind = start + WAIT_REBIND_S
    seen: int | None = None
    msgs: list[dict] = []
    while True:
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        if version != seen:
            seen = version
            msgs = store.inbox_after(
                conn, sid, after_id=args.after, urgent_only=args.urgent_only, limit=args.limit
            )
            if msgs:
                break
        now = time.monotonic()
        if os.getppid() != parent:
            return 0
        if args.session and now >= next_rebind:
            next_rebind = now + WAIT_REBIND_S
            actor = guard.resolve_actor(conn).session_id
            if actor not in (sid, HUMAN_SESSION) and store.get_session(conn, actor) is not None:
                out = {"rebound": True, "session_id": actor, "cursor": store.max_message_id(conn)}
                print(json.dumps(out), flush=True)
                return 0
        if now - start >= args.timeout:
            break
        time.sleep(args.interval)
    cursor = msgs[-1]["id"] if msgs else args.after
    rows = [_pull_row(m, args.preview_chars, wakes=_wakes(conn, sid, m)) for m in msgs]
    print(json.dumps({"session_id": sid, "cursor": cursor, "messages": rows}), flush=True)
    return 0


def cmd_send(args) -> int:
    body = " ".join(args.text).strip()
    if not body:
        print("nothing to send", file=sys.stderr)
        return 1
    if (args.to is None) == (args.topic is None):
        print("send needs exactly one of --to or --topic", file=sys.stderr)
        return 1
    conn = store.connect()
    sid = _resolve_me(conn, args)
    if sid is None:
        return 1
    row = store.get_session(conn, sid)
    to_session = None
    topic = None
    if args.to:
        to_session = _resolve(conn, args.to)
        if to_session is None:
            print(f"no agent matches '{args.to}' — try `agents`", file=sys.stderr)
            return 1
    else:
        topic = args.topic.lstrip("#")
        store.ensure_topic(conn, topic, created_by=sid)
    # Same cross-project consent + gated-topic gate the MCP `send` tool enforces
    # (issue #26): a session-identity CLI send must not be a back door.
    err = store.consent_error(conn, sid, to_session=to_session, topic=topic)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    mid = store.post_message(
        conn,
        from_session=sid,
        from_handle=row["handle"] if row else None,
        body=body,
        to_session=to_session,
        topic=topic,
        project=row["project"] if row else None,
        urgent=args.urgent,
    )
    dest = to_session or f"#{topic}"
    if args.json:
        print(json.dumps({"id": mid, "from": sid, "to": dest, "urgent": bool(args.urgent)}))
    else:
        print(f"{C.GREEN}sent{C.OFF} #{mid} → {dest}")
    return 0


def cmd_mark_read(args) -> int:
    conn = store.connect()
    sid = _resolve_me(conn, args)
    if sid is None:
        return 1
    ids = None
    if args.ids:
        try:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            print(f"--ids must be comma-separated integers: {args.ids!r}", file=sys.stderr)
            return 1
    n = store.mark_read(conn, sid, message_ids=ids, all_unread=args.all)
    if args.json:
        print(json.dumps({"marked": n}))
    else:
        print(f"marked {n}")
    return 0


def cmd_meter(args) -> int:
    """Record one push-triggered event in `hook_metrics` so `standup` can audit
    the cost of auto-turns ("turns triggered by push").
    """
    conn = store.connect()
    sid = _resolve_me(conn, args)
    if sid is None:
        return 1
    store.record_hook_metric(
        conn, session_id=sid, event=args.event, injected_chars=args.chars, elapsed_ms=None
    )
    if args.json:
        print(json.dumps({"ok": True, "event": args.event, "session": sid}))
    else:
        print(f"metered {args.event} for {sid}")
    return 0


def cmd_install(args) -> int:
    """Wire cabildo into the client(s): MCP server, hooks, and the push mod. Uses the
    venv this command runs from, so it works from a pip install; re-running is a no-op.
    """
    from . import install

    clients = {"both": ("claude", "codex"), "all": ("claude", "codex", "opencode")}.get(
        args.client, (args.client,)
    )
    return install.run(clients, print_only=args.print_only)


def cmd_plugin(args) -> int:
    """Install (or remove) the bundled Claude Code mod — push delivery, #30 —
    at user scope via `claude plugin`, the same steps `make install` runs. The
    MCP server and hooks give an agent cabildo inside a turn; this mod adds the
    doorbell that wakes an idle session when a message lands. Non-fatal: if
    `claude` is missing or the manifest fails to validate, it says so and the
    bus still works without it.
    """
    from . import mods

    def run(argv: list[str]) -> int:
        print(f"  {C.DIM}$ {' '.join(shlex.quote(c) for c in argv)}{C.OFF}")
        return subprocess.call(argv)

    if args.print_only:
        where = mods.mods_dir()
        print(f"{C.DIM}mod marketplace root:{C.OFF} {where or '(none found on this install)'}")
        print(f"{C.DIM}would run (scope {args.scope}):{C.OFF}")
        for argv in mods.plan(scope=args.scope, remove=args.remove):
            print(f"    $ {' '.join(shlex.quote(c) for c in argv)}")
        return 0

    verb = "remove" if args.remove else "install"
    print(f"{C.BOLD}{verb} {mods.PLUGIN_ID}{C.OFF} {C.DIM}(scope {args.scope}){C.OFF}")
    res = mods.remove(run, scope=args.scope) if args.remove else mods.install(run, scope=args.scope)
    if not res.ok:
        print(f"{C.YELLOW}skipped:{C.OFF} {res.reason} {C.DIM}(the bus still works){C.OFF}")
        return 1
    # A step that actually failed must not hide behind the green line; the
    # wording (single-sourced in mods) stays honest about the re-run case.
    for line in mods.step_warnings(res):
        print(f"  {C.YELLOW}!{C.OFF} {line}")
    if args.remove:
        print(f"{C.GREEN}removed{C.OFF} — restart Claude Code sessions to drop the doorbell")
    else:
        print(
            f"{C.GREEN}installed + enabled{C.OFF} — new sessions load it; restart a running "
            f"session to pick it up. It shells {C.BOLD}cabildo{C.OFF}, so set "
            f"{C.DIM}env.CABILDO_BIN{C.OFF} if cabildo is not on the session's PATH."
        )
    return 0


_LEVEL_COLOR = {"critical": C.RED, "warn": C.YELLOW, "info": C.CYAN}
# Robot codenames run long (quantum-cortana-ba22c); one column width for all of
# them beats every row wrapping differently.
_W = 22


def _parse_stop_time(s: str) -> float | None:
    """A wall-clock stop time (``23:00``, ``23``, ``11pm``, ``9:30pm``) as an
    epoch for today, rolled to tomorrow if it has already passed. None if it
    doesn't parse — the caller reports it rather than guessing a time.
    """
    raw = s.strip().lower().replace(" ", "")
    ampm = None
    if raw.endswith("am") or raw.endswith("pm"):
        ampm, raw = raw[-2:], raw[:-2]
    try:
        if ":" in raw:
            hh, mm = raw.split(":", 1)
            hour, minute = int(hh), int(mm)
        else:
            hour, minute = int(raw), 0
    except ValueError:
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    # Naive local time: a fixed-offset aware datetime keeps today's UTC offset,
    # so "tomorrow 23:00" across a DST change would land an hour off.
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def cmd_stop(args) -> int:
    """Set the fleet's wind-down line: the time you mean to stop.

    ``cabildo stop 23:00`` tells every active session on this machine to wind
    down as 23:00 nears — a gentle nudge at ~45 min, firmer inside 15, and once
    past the line. Schedule-anchored, so it never fires in the afternoon; and
    advisory, so it never blocks a tool call. ``--project`` scopes the line to
    this repo instead of the whole fleet; ``--clear`` stands it down.
    """
    from .context import git_project

    conn = store.connect()
    scope = "global"
    if getattr(args, "project", False):
        try:
            path, _ = git_project(os.getcwd())
        except Exception:
            path = None
        if not path:
            print("--project: not inside a known project", file=sys.stderr)
            return 1
        scope = path

    if args.clear:
        n = store.clear_stops(conn, scope=None if args.all else scope)
        where = (
            "every scope" if args.all else ("this project" if scope != "global" else "the fleet")
        )
        print(f"stood down {n} stop line(s) for {where}" if n else "no active stop line to clear")
        return 0

    if not args.time:
        rows = store.active_stops(conn)
        if not rows:
            print(f"{C.DIM}no stop line set on {store.hostname()}{C.OFF}")
            return 0
        now = time.time()
        for r in rows:
            at = datetime.fromtimestamp(r["line_at"]).astimezone()
            dim = C.DIM
            delta = r["line_at"] - now
            when = f"in {_ago(delta)}" if delta > 0 else f"{_ago(-delta)} ago"
            label = "fleet" if r["scope"] == "global" else r["scope"]
            print(f"{C.BOLD}{at:%H:%M}{C.OFF} ({when}) · {label}")
            told = conn.execute(
                "SELECT COUNT(*) n FROM schedule_deliveries WHERE schedule_id=?", (r["id"],)
            ).fetchone()["n"]
            print(
                f"    {dim}set {_ago(now - r['created_at'])} ago · nudged {told} session(s){C.OFF}"
            )
        return 0

    line_at = _parse_stop_time(args.time)
    if line_at is None:
        print(f"could not read a time from {args.time!r} (try 23:00, 11pm, 23)", file=sys.stderr)
        return 1
    row = store.set_stop(conn, line_at=line_at, scope=scope, created_by=f"@{HUMAN_HANDLE}")
    at = datetime.fromtimestamp(row["line_at"]).astimezone()
    where = "this project" if scope != "global" else "every session on this machine"
    print(
        f"stop set for {C.BOLD}{at:%H:%M}{C.OFF} ({at:%a %d %b}) — {where} winds down "
        f"as it nears (nudge at ~45m, 15m, and past the line)"
    )
    print(f"{C.DIM}stand it down with: cabildo stop --clear{C.OFF}")
    return 0


def cmd_mem(args) -> int:
    """This machine's memory, attributed to agents.

    The point of the attribution is that `top` cannot do it: it shows you a
    7 GB python and leaves you to work out whose it is and whether anyone is
    still waiting for it.
    """
    from . import monitor, procs

    conn = store.connect()
    live = store.active_sessions(conn)
    pid_to_session = {r["claude_pid"]: r["session_id"] for r in live if r["claude_pid"]}
    live_ids = {r["session_id"] for r in live}
    snap = procs.snapshot(min_rss_mb=args.min_mb, pid_to_session=pid_to_session)
    loose = [p for p in procs.loose(snap, live_ids) if not procs.is_self(p)]

    def who_of(p) -> str:
        """Plain text, no escapes: these get %-padded into columns, and ANSI
        inside a padded field misaligns every row that uses it.
        """
        if p.session_id:
            return store.display_name(store.get_session(conn, p.session_id), p.session_id)
        return f"claude {p.claude_pid}" if p.claude_pid else "—"

    if not args.loose:
        m = snap.memory
        th = monitor.Thresholds()
        level = monitor.level_for(snap, th)
        col = _LEVEL_COLOR.get(level or "", C.GREEN)
        print(
            f"{col}{C.BOLD}{m.avail_pct:.0f}% available{C.OFF} "
            f"({m.available_kb / 1048576:.1f}G of {m.total_kb / 1048576:.1f}G)"
            + (
                f" · swap {m.swap_used_pct:.0f}% of {m.swap_total_kb / 1048576:.0f}G"
                if m.swap_total_kb
                else ""
            )
            + (
                f" · stalled {snap.stall_pct:.1f}% of the last minute"
                if snap.pressure
                else f" {C.DIM}· no PSI ({'Linux-only' if sys.platform != 'linux' else 'not in this kernel'}){C.OFF}"
            )
            + (f" → {col}{level}{C.OFF}" if level else "")
        )

        by_session = snap.by_session()
        if by_session:
            ranked = sorted(by_session.items(), key=lambda kv: -sum(p.rss_kb for p in kv[1]))
            agents_kb = sum(p.rss_kb for p in snap.procs if p.session_id)
            print(
                f"\n{C.BOLD}by agent{C.OFF} {C.DIM}— {len(ranked)} agent(s) holding "
                f"{agents_kb / 1048576:.1f}G of the {sum(p.rss_kb for p in snap.procs) / 1048576:.1f}G "
                f"in processes over {args.min_mb}M{C.OFF}"
            )
            for sid, plist in ranked[: args.top]:
                total = sum(p.rss_kb for p in plist) / 1048576.0
                row = store.get_session(conn, sid)
                name = store.display_name(row, sid)
                state = "" if row and row["status"] == "active" else f" {C.RED}(gone){C.OFF}"
                print(
                    f"  {_sender_color(name)}{name:<{_W}}{C.OFF} {total:5.1f}G  "
                    f"{len(plist)} proc(s){state}"
                )
                for p in sorted(plist, key=lambda p: -p.rss_kb)[:3]:
                    print(f"      {C.DIM}pid {p.pid:<8} {p.rss_mb:>5}M  {p.label(70)}{C.OFF}")
            if len(ranked) > args.top:
                rest = sum(p.rss_kb for _, pl in ranked[args.top :] for p in pl)
                print(
                    f"  {C.DIM}… and {len(ranked) - args.top} more, "
                    f"{rest / 1048576:.1f}G between them{C.OFF}"
                )

        print(f"\n{C.BOLD}biggest processes{C.OFF}")
        for p in snap.procs[: args.top]:
            print(f"  {p.rss_mb:>6}M  {who_of(p):<{_W}} {C.DIM}{p.label(64)}{C.OFF}")

    if loose:
        total = sum(p.rss_kb for p in loose) / 1048576.0
        print(
            f"\n{C.RED}{C.BOLD}left behind{C.OFF} — {len(loose)} process(es), "
            f"{total:.1f}G, owned by agents that are gone"
        )
        now = time.time()
        for p in sorted(loose, key=lambda p: -p.rss_kb):
            age = _ago(now - p.started_at) if p.started_at else "?"
            print(
                f"  {p.rss_mb:>6}M  pid {p.pid:<8} {C.DIM}{age:>4} old  "
                f"{who_of(p):<{_W}} {p.label(52)}{C.OFF}"
            )
        print(
            f"{C.DIM}nothing here is killed for you. `kill {loose[0].pid}` etc., "
            f"once you recognize them.{C.OFF}"
        )
    elif args.loose:
        print(f"{C.DIM}nothing left behind{C.OFF}")
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
    bus closed reads as ended.
    """
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
    tab from one your laptop took down.
    """
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


def _fmt_tokens(chars: int) -> str:
    from .transcript import CHARS_PER_TOKEN

    return f"{chars // CHARS_PER_TOKEN:,} tok"


def cmd_cost(args) -> int:
    """What running cabildo costs: context it injects (hook_metrics), the hook
    latency per tool call, and the tokens agents spend calling its tools
    (scanned from Claude Code transcripts).
    """
    from . import transcript
    from .transcript import CHARS_PER_TOKEN

    since = time.time() - args.days * 86400 if args.days else None
    window = f"last {args.days}d" if args.days else "all time"
    conn = store.connect()

    # --- 1 & 2: passive overhead cabildo pushes into sessions + hook latency ---
    m = store.hook_metrics_summary(conn, since=since, session_id=args.session)
    t = m["total"]
    print(
        f"{C.BOLD}cabildo cost · {window}"
        f"{' · session ' + args.session[:8] if args.session else ''}{C.OFF}\n"
    )

    print(
        f"{C.BOLD}1. Context injected by hooks{C.OFF} "
        f"{C.DIM}(banners, unread lines — billed every turn while in context){C.OFF}"
    )
    if t["fires"]:
        print(
            f"   {t['chars']:,} chars ≈ {_fmt_tokens(t['chars'])} "
            f"over {t['fires']:,} hook fires, {t['sessions']} session(s)"
        )
        print(
            f"\n{C.BOLD}2. Hook latency{C.OFF} "
            f"{C.DIM}(a subprocess per hook fire; the recurring cost when quiet){C.OFF}"
        )
        print(
            f"   {'event':<16}{'fires':>8}{'injected':>12}{'chars':>10}{'avg ms':>9}{'total s':>10}"
        )
        for e in m["per_event"]:
            print(
                f"   {e['event']:<16}{e['fires']:>8,}{e['injections']:>12,}"
                f"{e['chars']:>10,}{(e['avg_ms'] or 0):>9.1f}{(e['total_ms'] or 0) / 1000:>10.1f}"
            )
    else:
        print(
            f"   {C.DIM}no hook_metrics yet — the instrumented hooks must be "
            f"installed & a session run before data appears.{C.OFF}"
        )

    # --- 3: tokens agents spend calling cabildo tools (from transcripts) ---
    print(
        f"\n{C.BOLD}3. Agent tool traffic{C.OFF} "
        f"{C.DIM}(cabildo tool calls + results, scanned from transcripts — approx){C.OFF}"
    )
    tr = transcript.scan(session_id=args.session)
    if tr["calls"]:
        print(
            f"   {tr['calls']:,} calls across {tr['files']} transcript(s): "
            f"{tr['input_chars']:,} in + {tr['output_chars']:,} out chars "
            f"≈ {tr['tokens_est']:,} tok"
        )
        print(f"\n   {'tool':<28}{'calls':>8}{'in tok':>10}{'out tok':>10}")
        for name, e in sorted(
            tr["by_tool"].items(), key=lambda kv: -(kv[1]["input_chars"] + kv[1]["output_chars"])
        ):
            short = name.replace("mcp__cabildo__", "")
            print(
                f"   {short:<28}{e['calls']:>8,}"
                f"{e['input_chars'] // CHARS_PER_TOKEN:>10,}"
                f"{e['output_chars'] // CHARS_PER_TOKEN:>10,}"
            )
    else:
        print(f"   {C.DIM}no cabildo tool calls found in transcripts.{C.OFF}")

    if args.rate:
        total_tok = t["chars"] // CHARS_PER_TOKEN + tr["tokens_est"]
        print(
            f"\n{C.BOLD}≈ €{total_tok / 1_000_000 * args.rate:.4f}{C.OFF} "
            f"{C.DIM}at €{args.rate}/1M tok (rough; ignores prompt caching, "
            f"which makes re-sent context much cheaper){C.OFF}"
        )
    else:
        print(
            f"\n{C.DIM}Pass --rate <€ per 1M tokens> for a € estimate. Caveat: "
            f"prompt caching makes re-sent context far cheaper than raw tokens.{C.OFF}"
        )
    return 0


def cmd_browser(args) -> int:
    """``cabildo browser``: status, reap, doctor of the browser agents use."""
    if args.browser_cmd == "serve":
        from .browser import server as browser_server

        if args.headless or args.user_data_dir:
            print(
                "cabildo browser serve: the browser is always visible and isolated, "
                "so --headless and --user-data-dir are refused",
                file=sys.stderr,
            )
            return 2
        return browser_server.serve(
            browser_server.Session(
                channel=args.browser or args.channel,
                storage_state=Path(args.storage_state).expanduser() if args.storage_state else None,
                output_dir=(
                    Path(args.output_dir).expanduser()
                    if args.output_dir
                    else browser_server.Session().output_dir
                ),
            )
        )

    from . import procs
    from .browser import entry as browser_entry
    from .browser import fleet as browser_fleet
    from .browser import report as browser_report

    try:
        entry = browser_entry.load_entry()
    except ValueError as exc:
        print(f"cabildo browser: {exc}", file=sys.stderr)
        return 1
    saved = entry.flags.storage_state if entry else None
    state = browser_entry.read_storage_state(
        Path(saved).expanduser() if saved else browser_entry.DEFAULT_STATE
    )

    if args.browser_cmd == "login":
        from .browser import login as browser_login

        saved = browser_login.run(
            Path(args.profile).expanduser() if args.profile else browser_login.default_profile(),
            state.path,
            channel=args.channel,
            say=print,
        )
        if saved is None:
            return 1
        print(f"saved {saved.cookies} cookies, {saved.origins} origins to {saved.path} (mode 600)")
        print("Agent browsers pick it up on their next tool call.")
        return 0

    if args.browser_cmd == "doctor":
        findings = browser_entry.check(entry, state)
        print("\n".join(browser_report.doctor(findings)))
        return 1 if any(f.level == "fail" for f in findings) else 0

    processes = procs.processes()
    fleet = browser_fleet.find(processes)
    if args.browser_cmd == "status":
        print("\n".join(browser_report.status(fleet, state, now=time.time())))
        return 0

    protect = browser_fleet.ancestors(os.getpid(), processes)
    targets = browser_fleet.pick(
        fleet, everything=args.all, include_mine=args.include_mine, protect=protect
    )
    if args.dry_run:
        print("\n".join(browser_report.reap_plan(targets)))
        return 0
    print("\n".join(browser_report.reap_result(browser_fleet.stop(targets))))
    return 0


def _age(secs: float) -> str:
    s = int(secs)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d"


def _branch_line_stats(e: dict) -> str:
    """The shared middle columns for a worktree or a bare branch: ahead/behind and
    whether it is already contained in the trunk.
    """
    ab = "?" if e["ahead"] is None else f"+{e['ahead']} -{e['behind']}"
    trunk = f" {C.DIM}in-trunk{C.OFF}" if e.get("in_trunk") else ""
    return f"{ab:>9}{trunk}"


def _agent_label(agent: dict | None) -> str:
    if not agent:
        return f"{C.DIM}no agent{C.OFF}"
    state = agent.get("presence") or agent.get("status") or ""
    col = C.GREEN if state in ("working", "registered") else C.DIM
    return f"{col}{agent['name']}{C.OFF} {C.DIM}({state}){C.OFF}"


def cmd_wan(_args) -> int:
    """Print the WAN peers the daemon syncs with, and their last pull."""
    from . import wan

    report = wan.peers_report()
    if not report:
        print(
            f"{C.DIM}no WAN peers: set [project.<name>.remote] in {store.home()}/config.toml, "
            f"then restart the daemon{C.OFF}"
        )
        return 0
    for r in report:
        last = r["last_pull"]
        state = "not pulled yet" if last is None else last["error"] or f"cursor {last['cursor']}"
        print(f"{r['project']}  {r['address']}  {state}")
    return 0


def _known_projects() -> list[str]:
    """Every project name the local bus has seen — the first-class list of
    'folders' the user coordinates in — best-effort, read-only.
    """
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
                "WHERE project_name IS NOT NULL AND project_name<>''"
            ).fetchall()
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
    on the local bus. With no args, lists every known project and its dials.
    """
    from . import cloud

    if args.default is not None:
        lvl = cloud.set_default_uplink(args.default)
        print(
            f"{C.GREEN}default uplink{C.OFF} → {C.CYAN}{lvl}{C.OFF} "
            f"{C.DIM}({_UPLINK_BLURB.get(lvl, '')}){C.OFF}"
        )
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
            print(
                f"{C.GREEN}{name}{C.OFF} uplink → {C.CYAN}{lvl}{C.OFF} "
                f"{C.DIM}({_UPLINK_BLURB.get(lvl, '')}){C.OFF}"
            )
        if args.link:
            cloud.add_peer(name, args.link.strip().lower())
            print(
                f"{C.GREEN}linked{C.OFF} {name} ↔ {args.link.strip().lower()} "
                f"{C.DIM}(they can see each other on the bus){C.OFF}"
            )
        if args.unlink:
            cloud.remove_peer(name, args.unlink.strip().lower())
            print(f"{C.YELLOW}unlinked{C.OFF} {name} ✗ {args.unlink.strip().lower()}")
        # Always show the project's resulting state.
        eff = cloud.uplink_of(name)
        peers = cloud.peers_of(name)
        listed = name in cloud.project_registry()
        tag = "" if listed else f" {C.DIM}(default){C.OFF}"
        print(f"\n{C.BOLD}{name}{C.OFF}")
        print(f"  uplink  {C.CYAN}{eff}{C.OFF}{tag} {C.DIM}— {_UPLINK_BLURB.get(eff, '')}{C.OFF}")
        print(
            f"  peers   {C.CYAN}{', '.join(peers) if peers else '—'}{C.OFF} "
            f"{C.DIM}(local bus visibility){C.OFF}"
        )
        roots = cloud.roots_of(name)
        print(
            f"  folders {C.CYAN}{', '.join(roots) if roots else '—'}{C.OFF} "
            f"{C.DIM}(pinned with `cabildo add`){C.OFF}"
        )
        return 0

    # No project → the whole board.
    reg = cloud.project_registry()
    default = cloud.default_uplink()
    names = sorted(set(reg) | set(_known_projects()))
    print(f"{C.BOLD}projects{C.OFF}  {C.DIM}default uplink: {C.OFF}{C.CYAN}{default}{C.OFF}")
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
    print(
        f"\n{C.DIM}* = using the default; set with `cabildo project <name> --uplink <level>`{C.OFF}"
    )
    return 0


def _hooks_installed() -> bool:
    """Whether ~/.claude/settings.json runs cabildo-hook. The MCP server alone
    gives an agent the tools; the hooks are what make it join, be seen, and be
    held to its claims — so a folder wired with only half of that is worth
    saying out loud.
    """
    try:
        text = Path(os.path.expanduser("~/.claude/settings.json")).read_text()
    except OSError:
        return False
    return "cabildo-hook" in text


def cmd_add(args) -> int:
    """Put a folder on the bus: pin its project name for this machine and wire
    its .mcp.json, so every Claude Code session opened there joins cabildo
    without anyone remembering to configure anything.
    """
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
        print(
            f"{C.YELLOW}unregistered{C.OFF} {root}"
            + (f" {C.DIM}(was {owner}){C.OFF}" if owner else f" {C.DIM}(was not registered){C.OFF}")
        )
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
        print(
            f"  {C.DIM}git would have called this {git_name!r}; pinned to "
            f"{name!r} for every session here{C.OFF}"
        )
    if root != target:
        print(
            f"  {C.DIM}resolved up to the repo root (use --exact to register "
            f"{target} itself){C.OFF}"
        )

    if not args.no_mcp:
        try:
            p, what = mcp_config.apply(root, name)
        except (ValueError, OSError) as e:
            print(f"  {C.RED}could not write .mcp.json: {e}{C.OFF}", file=sys.stderr)
            return 1
        wired = "cabildo " + C.DIM + "(bus + issues/agenda tools)" + C.OFF
        color = C.DIM if what == "unchanged" else C.GREEN
        print(f"  {color}{what}{C.OFF} {p} {C.DIM}—{C.OFF} {wired}")
        if not mcp_config.has_issues(root):
            print(
                f"  {C.DIM}no docs/issues/ here yet; the register tools still load. "
                f"`cabildo issues init {root}` starts one.{C.OFF}"
            )
        print(
            f"  {C.DIM}Claude Code asks once to approve a project MCP server; "
            f".mcp.json is meant to be committed — gitignore it if this wiring "
            f"is yours alone.{C.OFF}"
        )

    eff = cloud.uplink_of(name)
    peers = cloud.peers_of(name)
    print(
        f"  {C.DIM}uplink{C.OFF} {C.CYAN}{eff}{C.OFF} {C.DIM}— {_UPLINK_BLURB.get(eff, '')}{C.OFF}"
    )
    if peers:
        print(f"  {C.DIM}peers{C.OFF}  {C.CYAN}{', '.join(peers)}{C.OFF}")
    if not _hooks_installed():
        print(
            f"\n{C.YELLOW}note{C.OFF} no cabildo hooks in ~/.claude/settings.json — "
            f"sessions here will have the tools but won't auto-join, be listed, "
            f"or have claims enforced. Run {C.BOLD}python scripts/install.py{C.OFF} once."
        )
    print(f"\n{C.DIM}restart Claude Code in that folder to pick it up.{C.OFF}")
    return 0


def cmd_codex(args) -> int:
    """Wire Codex onto the same bus as Claude Code. Delegates to `codex mcp add`
    when Codex is installed; otherwise prints the `~/.codex/config.toml` block to
    paste. Preserves the existing Claude flow — this touches only Codex config.
    """
    import subprocess

    from . import codex_config, store
    from .context import _git_project

    target = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(target):
        print(f"{C.RED}not a directory:{C.OFF} {target}", file=sys.stderr)
        return 2
    _, git_name = _git_project(Path(target))
    project = (args.project or git_name).strip().lower()
    home = str(store.home())
    user_cfg, proj_cfg = codex_config.config_paths()

    if args.remove:
        if codex_config.codex_on_path():
            res = subprocess.run(codex_config.remove_argv(), capture_output=True, text=True)
            out = (res.stdout or res.stderr).strip()
            print(f"{C.YELLOW}codex mcp remove cabildo{C.OFF} {C.DIM}—{C.OFF} {out or 'done'}")
        else:
            print(
                f"{C.DIM}codex not on PATH; remove the {C.OFF}[mcp_servers.cabildo]{C.DIM} "
                f"block from {user_cfg} yourself.{C.OFF}"
            )
        hp, hwhat = codex_config.remove_hooks()
        print(f"{C.YELLOW}hooks{C.OFF} {C.DIM}—{C.OFF} {hp} ({hwhat})")
        return 0

    argv = codex_config.add_argv(project, home)
    block = codex_config.toml_block(project, home)

    if args.print_only:
        print(f"{C.DIM}would run:{C.OFF} {' '.join(argv)}")
        print(f"\n{C.DIM}or paste into {user_cfg} (or {proj_cfg} for this repo only):{C.OFF}")
        print(block)
        print(f"{C.DIM}and merge these hooks into {codex_config.hooks_path()}:{C.OFF}")
        print(codex_config.hooks_preview())
        return 0

    if codex_config.codex_on_path():
        res = codex_config.run_add(project, home)
        if res.returncode == 0:
            print(f"{C.GREEN}registered{C.OFF} cabildo with Codex {C.DIM}(codex mcp add){C.OFF}")
        else:
            print(f"{C.RED}codex mcp add failed:{C.OFF} {(res.stderr or res.stdout).strip()}")
            print(f"{C.DIM}paste this into {user_cfg} instead:{C.OFF}\n{block}")
            return 1
    else:
        print(f"{C.YELLOW}codex not on PATH{C.OFF} — paste this into {C.BOLD}{user_cfg}{C.OFF}:")
        print(block)
        print(f"{C.DIM}(or {proj_cfg} for this repo only; project scope must be trusted).{C.OFF}")

    print(
        f"  {C.DIM}CABILDO_CLIENT=codex tags this session as Codex on the bus; "
        f"CABILDO_HOME pins the shared DB. Restart Codex to pick it up.{C.OFF}"
    )

    try:
        hp, hwhat = codex_config.apply_hooks()
        color = C.DIM if hwhat == "unchanged" else C.GREEN
        print(f"  {color}{hwhat}{C.OFF} {hp} {C.DIM}— delivery + claim-guard hooks{C.OFF}")
        print(
            f"  {C.DIM}Codex requires trust for hooks: run {C.OFF}/hooks{C.DIM} in Codex to "
            f"review and trust them (project scope must be trusted too).{C.OFF}"
        )
    except (ValueError, OSError) as e:
        print(f"  {C.RED}could not write hooks.json: {e}{C.OFF}", file=sys.stderr)
        return 1
    return 0


def cmd_opencode(args) -> int:
    """Wire opencode onto the same bus as Claude Code and Codex. Edits opencode's
    JSON config directly (only cabildo's own entry, tagged opencode) and installs the
    /agenda + /sanity commands. No hooks yet: opencode gets messaging, presence and
    advisory claims, but not hook-enforced file leases.
    """
    from . import opencode_config, store
    from .context import _git_project

    target = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(target):
        print(f"{C.RED}not a directory:{C.OFF} {target}", file=sys.stderr)
        return 2
    _, git_name = _git_project(Path(target))
    project = (args.project or git_name or "").strip().lower()
    home = str(store.home())
    cfg = opencode_config.config_path()

    if args.remove:
        p, what = opencode_config.remove()
        print(f"{C.YELLOW}opencode{C.OFF} {C.DIM}—{C.OFF} {p} ({what}); commands removed")
        return 0

    if args.print_only:
        print(f"{C.DIM}would write {cfg}:{C.OFF}")
        print(opencode_config.preview(project, home))
        cmds = ", ".join(opencode_config.COMMANDS)
        print(f"{C.DIM}and install into {opencode_config.command_dir()}: {cmds}{C.OFF}")
        return 0

    try:
        p, what = opencode_config.apply(project, home)
    except (ValueError, OSError) as e:
        print(f"{C.RED}could not write {cfg}: {e}{C.OFF}", file=sys.stderr)
        return 1
    color = C.DIM if what == "unchanged" else C.GREEN
    print(f"  {color}{what}{C.OFF} {p} {C.DIM}— cabildo MCP, tagged opencode{C.OFF}")
    print(
        f"  {C.GREEN}installed{C.OFF} {opencode_config.command_dir()} "
        f"{C.DIM}— /agenda, /sanity{C.OFF}"
    )
    print(
        f"  {C.DIM}CABILDO_CLIENT=opencode tags this session on the bus; CABILDO_HOME pins "
        f"the shared DB. Restart opencode to pick it up.{C.OFF}"
    )
    print(
        f"  {C.DIM}no hook-enforced file leases in opencode yet — messaging, presence and "
        f"advisory claims only.{C.OFF}"
    )
    return 0


def cmd_session_ops(args) -> int:
    """`cabildo sessions <cmd>`: find, read, tag and trash Claude Code sessions."""
    from . import sessions_ops

    root = None if args.all else os.path.abspath(args.dir)
    try:
        if args.sessions_cmd == "list":
            found = sessions_ops.rows(root, tag=args.tag, query=args.query, contains=args.contains)
            for r in found:
                d = sessions_ops.describe(r)
                tags = ",".join(r.meta.tags) or "-"
                print(f"{str(d['id'])[:8]}  {d['date']}  {tags:20}  {d['title']}")
            return 0
        rows = sessions_ops.select(root, ids=args.ids, tag=args.tag)
        if args.sessions_cmd == "read":
            for r in rows:
                print(f"# {r.display_title}\n")
                for t in sessions_ops.conversation(r):
                    print(f"## {t.role} {t.timestamp[:16]}\n\n{t.text}\n")
        elif args.sessions_cmd == "tag":
            n = sessions_ops.tag(rows, args.add, args.remove)
            print(f"{n} session(s) changed")
        else:
            for d in sessions_ops.trash(rows, force=args.force):
                print(d)
    except sessions_ops.SessionsError as e:
        print(f"cabildo sessions: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_minutes(args) -> int:
    """The day's conversation minutas for a project, distilled from its transcripts."""
    import datetime as _dt

    from . import minutes
    from .context import git_project

    root, name = git_project(os.getcwd())
    project_name = args.project or name
    day = args.date or _dt.date.today().isoformat()
    text = minutes.render_project_day(project_name, root, day)
    if args.write:
        out = minutes._day_file(project_name, day)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text if text.endswith("\n") else text + "\n")
        print(f"{C.GREEN}wrote{C.OFF} {out}")
    else:
        print(text)
    return 0


def cmd_messages(args) -> int:
    """Recorded conversation turns from the bus DB. Needs recording on for the
    project ([record] messages) and a daemon started with --save-messages.
    Filter by project, session, role, or a substring; newest first, printed
    oldest-first so a run reads in order.
    """
    import datetime as _dt

    from . import store
    from .context import git_project

    if args.import_all:
        from . import record, transcript
        from .workspace.sessions import trash_dir

        with store.connect() as conn:
            n = record.import_all(conn, (transcript.transcript_root(), trash_dir()), log=print)
        print(f"imported {n} new turn(s)")
        return 0
    _root, name = git_project(os.getcwd())
    project = None if args.all else (args.project or name)
    since = _dt.datetime.now().timestamp() - args.since * 3600.0 if args.since else None
    with store.connect() as conn:
        rows = store.query_transcript_messages(
            conn,
            project_name=project,
            session_id=args.session,
            role=args.role,
            grep=args.grep,
            since=since,
            limit=args.limit,
        )
    if not rows:
        where = "" if args.all else f" for {project}"
        print(
            f"{C.DIM}no recorded messages{where} — turn it on with [record] messages = true "
            f"and run the daemon with --save-messages.{C.OFF}"
        )
        return 0
    for r in reversed(rows):
        when = _dt.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M") if r["ts"] else "  ?  "
        who = "🧑" if r["role"] == "user" else "🤖"
        model = f" {C.DIM}{r['model']}{C.OFF}" if r["model"] else ""
        proj = f" {C.DIM}[{r['project_name']}]{C.OFF}" if args.all else ""
        body = " ".join((r["body"] or "").split())[:200]
        print(f"{C.DIM}{when}{C.OFF} {who}{model}{proj} {body}")
    return 0


def cmd_notes(args) -> int:
    """The human's seat at the session notes (issue #34): the loose ends agents
    captured with `note_add`, scoped to the session that raised them. Lists this
    session's open notes by default (resolve the session the way the git hook
    does, or pass --session); --done/--park triage one by id from the terminal.
    """
    import datetime as _dt

    conn = store.connect()
    if args.add:
        sid = _resolve_me(conn, args)
        if sid is None:
            return 1
        me = store.get_session(conn, sid)
        nid = store.add_note(
            conn,
            text=args.add,
            type=args.type or "topic",
            session_id=sid,
            handle=me["handle"] if me else None,
            project=me["project"] if me else None,
            project_name=me["project_name"] if me else None,
            source="human",
        )
        print(f"{C.GREEN}noted{C.OFF} #{nid} for {sid[:8]}: {args.add}")
        return 0
    if args.done is not None or args.park is not None:
        nid = args.done if args.done is not None else args.park
        state = "done" if args.done is not None else "parked"
        row = store.set_note_state(conn, nid, state=state, parked_ref=args.ref)
        if row is None:
            print(f"no note #{nid}", file=sys.stderr)
            return 1
        print(f"{C.GREEN}{state}{C.OFF} #{nid}: {row['text']}")
        return 0

    sid = _resolve_me(conn, args) if not args.all else None
    if sid is None and not args.all:
        return 1
    rows = store.list_notes(
        conn,
        session_id=sid,
        state=(args.state or None),
        type=args.type,
        limit=args.limit,
    )
    if not rows:
        scope = "any session" if args.all else (sid or "this session")
        print(
            f"{C.DIM}no {args.state or ''} notes for {scope} — agents add them with note_add.{C.OFF}"
        )
        return 0
    glyph = {"open": "○", "done": "✓", "parked": "→"}
    for r in rows:
        when = _dt.datetime.fromtimestamp(r["created_at"]).strftime("%m-%d %H:%M")
        g = glyph.get(r["state"], "·")
        who = f" {C.DIM}{r['handle']}{C.OFF}" if r["handle"] else ""
        ref = f" {C.DIM}→ {r['parked_ref']}{C.OFF}" if r["parked_ref"] else ""
        print(
            f"{C.DIM}{when}{C.OFF} {g} {C.BOLD}#{r['id']}{C.OFF} "
            f"{C.CYAN}{r['type']}{C.OFF}{who}: {r['text']}{ref}"
        )
    return 0


# --------------------------------------------------------------------------- #
# The project register
# --------------------------------------------------------------------------- #
def cmd_issues(args) -> int:
    """The human's seat at a project's `docs/issues/` — same module the `issues`
    MCP server calls, so what you see here is what an agent sees.
    """
    from . import issues as reg

    start = Path(os.path.expanduser(args.folder)).resolve()
    folder = reg.find_folder(start)

    if args.action == "init":
        folder = folder or start / "docs" / "issues"
        cfg = folder / reg.CONFIG_NAME
        if cfg.exists() and not args.force:
            print(f"{C.YELLOW}exists{C.OFF} {cfg} {C.DIM}(--force to rewrite){C.OFF}")
            return 1
        found, _ = reg.load_all(folder) if folder.is_dir() else ((), ())
        if found:
            # Adoption: describe the folder as it stands, so `check` passes before anything is
            # edited. Tightening afterwards is then a decision, not a migration forced by a tool.
            folder.mkdir(parents=True, exist_ok=True)
            cfg.write_text(reg.infer_config(folder))
            print(f"{C.GREEN}wrote{C.OFF} {cfg}")
            print(
                f"  {C.DIM}inferred from {len(found)} issue(s) already here, so `check` passes "
                f"today. Tighten it deliberately.{C.OFF}"
            )
            return 0
        written = reg.scaffold(folder, areas=args.area or ())
        for p in written:
            print(f"{C.GREEN}wrote{C.OFF} {p}")
        if not written:
            print(f"{C.DIM}nothing to write — {folder} already has both{C.OFF}")
        if not args.area:
            print(
                f"  {C.YELLOW}no areas declared{C.OFF} {C.DIM}— until you set `areas` in "
                f"config.toml, any area is accepted and every typo becomes a new one. "
                f"Re-run with --area <name> --area <name>.{C.OFF}"
            )
        return 0

    if folder is None:
        print(
            f"{C.RED}no docs/issues/{C.OFF} at or above {start}\n"
            f"  {C.DIM}`cabildo issues init -C {start}` starts one{C.OFF}",
            file=sys.stderr,
        )
        return 2

    if args.action == "check":
        problems = reg.check(folder)
        for problem in problems:
            print(f"{C.RED}✗{C.OFF} {problem}", file=sys.stderr)
        if problems:
            print(f"\n{len(problems)} problem(s) in {folder}", file=sys.stderr)
            return 1
        found, _ = reg.load_all(folder)
        print(f"{C.GREEN}clean{C.OFF} {folder} {C.DIM}({len(found)} follow-ups){C.OFF}")
        return 0

    if args.action == "reindex":
        reg.reindex(folder)
        print(f"reindexed {folder / reg.README_NAME}")
        return 0

    if args.action == "show":
        if not args.name:
            print("show needs a name", file=sys.stderr)
            return 2
        print(reg.resolve(folder, args.name).read_text())
        return 0

    if args.action == "list":
        found, broken = reg.load_all(folder)
        for i in found:
            if args.status and i.status != args.status:
                continue
            if not args.status and not i.open:
                continue
            if args.area and i.area not in args.area:
                continue
            if args.owner and (i.owner or "none") != args.owner:
                continue
            owner = f" {C.CYAN}@{i.owner}{C.OFF}" if i.owner else ""
            print(
                f"{C.BOLD}{i.slug}{C.OFF}{owner}\n  {C.DIM}[{i.status}] {i.area}{C.OFF} — {i.title}"
            )
        for p in broken:
            print(f"{C.RED}unparseable{C.OFF} {p}", file=sys.stderr)
        return 0

    # ready — the default, and the question worth asking
    b = reg.board(folder, owner=args.owner)
    if b.ready:
        print(f"{C.GREEN}READY{C.OFF} {C.DIM}— nothing blocks these, nobody owns them{C.OFF}")
        for i in b.ready:
            print(f"  {C.BOLD}{i.slug}{C.OFF}\n    {i.title}\n    {C.DIM}{i.area}{C.OFF}")
    else:
        print(
            f"{C.YELLOW}nothing ready{C.OFF} {C.DIM}— every open issue is owned or blocked{C.OFF}"
        )
    if b.mine:
        print(f"\n{C.CYAN}YOURS{C.OFF}")
        for i in b.mine:
            print(f"  {C.BOLD}{i.slug}{C.OFF} {C.DIM}[{i.status}]{C.OFF} — {i.title}")
    if b.waiting:
        print(f"\n{C.DIM}WAITING ({len(b.waiting)}){C.OFF}")
        for w in b.waiting:
            print(f"  {C.DIM}{w.issue.slug} — {w.reason}{C.OFF}")
    if b.done:
        print(f"\n{C.DIM}{b.done} done.{C.OFF}")
    return 0


# --------------------------------------------------------------------------- #
# Background services
# --------------------------------------------------------------------------- #
def _service_units(args):
    """The unit `install` should write: the daemon."""
    from . import service

    return [
        service.daemon_unit(
            allow_push=args.allow_push,
            allow_spawn=args.allow_spawn,
            max_agents=args.max_agents,
            max_depth=args.max_depth,
            budget=args.budget,
            budget_days=args.budget_days,
            max_intent_age=args.max_intent_age,
            notify=not args.no_notify,
            save_messages=args.save_messages,
        )
    ]


def _service_selected(args):
    """Which unit a status/logs/restart/uninstall applies to: the daemon, whether
    or not it is installed, so status can report 'not installed' rather than
    nothing.
    """
    from . import service

    units = [service.daemon_unit()]
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
    """Install cabildo's daemon as a systemd --user service, so it survives
    closing the terminal.
    """
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
            print(
                f"{C.RED}refusing to overwrite unit files cabildo didn't write:{C.OFF}",
                file=sys.stderr,
            )
            for u in clash:
                print(
                    f"  {u.path}\n    {C.DIM}ExecStart={service.exec_start_of(u.path)}{C.OFF}",
                    file=sys.stderr,
                )
            print(
                f"\n{C.DIM}that one is doing a job this installer may not do the same "
                f"way — read it first, then --force to replace it.{C.OFF}",
                file=sys.stderr,
            )
            return 2
        print(
            f"{C.BOLD}installing{C.OFF} {', '.join(u.name for u in units)} "
            f"{C.DIM}→ {service.unit_dir()}{C.OFF}"
        )
        rc = _print_steps(service.install(units, start=not args.no_start, force=args.force))
        if args.linger:
            code, out = service.enable_linger()
            _print_steps([("enable-linger", code, out)])
        elif service.linger_enabled() is False:
            print(
                f"  {C.DIM}services stop when you log out; "
                f"`cabildo service install --linger` keeps them running.{C.OFF}"
            )
        daemon = next((u for u in units if u.key == service.DAEMON), None)
        if daemon and not args.allow_push and not args.allow_spawn:
            print(
                f"\n{C.YELLOW}dry-run daemon{C.OFF} {C.DIM}— it will report what it "
                f"WOULD do and push/spawn nothing. Re-run with --allow-push "
                f"--allow-spawn when you trust it.{C.OFF}"
            )
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
            rc, out = service.control(args.action, u)
            steps.append((f"{args.action} {u.name}", rc, out))
        return _print_steps(steps)

    if args.action == "logs":
        units = _service_selected(args)
        import subprocess

        if sys.platform == "darwin":
            # launchd has no journal: the agents write to files under log_dir().
            cmd = ["tail", "-n", str(args.lines)]
            if args.follow:
                cmd.append("-f")
            cmd += [str(service.log_path(u)) for u in units]
            try:
                return subprocess.call(cmd)
            except FileNotFoundError:
                print(f"{C.RED}no tail on PATH{C.OFF}", file=sys.stderr)
                return 2
        cmd = ["journalctl", "--user", "-n", str(args.lines)]
        for u in units:
            cmd += ["-u", u.name]
        if args.follow:
            cmd.append("-f")
        try:
            return subprocess.call(cmd)
        except FileNotFoundError:
            print(f"{C.RED}no journalctl on PATH{C.OFF}", file=sys.stderr)
            return 2

    # status (default)
    from . import service as sv

    print(f"{C.BOLD}cabildo services{C.OFF} {C.DIM}{sv.unit_dir()}{C.OFF}")
    any_installed = False
    for u in (sv.daemon_unit(),):
        st = sv.status(u)
        if not st["installed"]:
            print(f"  {C.DIM}○ {u.name:<24} not installed{C.OFF}")
            continue
        any_installed = True
        active = st.get("ActiveState", "?")
        col = C.GREEN if active == "active" else (C.RED if active == "failed" else C.YELLOW)
        since = st.get("ExecMainStartTimestamp") or ""
        extra = f"restarts {st['NRestarts']}" if st.get("NRestarts") not in (None, "0") else ""
        print(
            f"  {col}●{C.OFF} {C.BOLD}{u.name:<24}{C.OFF} {col}{active}"
            f"{C.OFF}/{st.get('SubState', '?')} "
            f"{C.DIM}{st.get('UnitFileState', '?')}"
            + (f" · since {since}" if since else "")
            + (f" · {extra}" if extra else "")
            + f"{C.OFF}"
        )
        if sv.foreign(u):
            print(
                f"      {C.YELLOW}not written by cabildo{C.OFF} "
                f"{C.DIM}— {sv.exec_start_of(u.path)}{C.OFF}"
            )
    if not any_installed:
        print(f"\n{C.DIM}nothing installed yet: {C.OFF}cabildo service install --allow-push{C.OFF}")
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
def cmd_viewer(args) -> int:
    from . import viewer

    return viewer.serve(args.host, args.port, args.open)


_DOCTOR_GLYPH = {
    "ok": (C.GREEN, "✓"),
    "warn": (C.YELLOW, "⚠"),
    "fail": (C.RED, "✗"),
    "na": (C.DIM, "○"),
}


# git-hook event -> the synthetic command the guard judges. A git pre-commit
# hook is a `git commit` about to happen; a pre-push hook, a `git push`.
_GITHOOK_COMMANDS = {"pre-commit": "git commit", "pre-push": "git push"}

# The daemon's own serialized push sets this on its subprocess, so a repo with
# [push].verify = true (which makes the daemon run this pre-push hook) never
# denies the daemon's own push — the deadlock Opus-5.5 review finding #1.
GITHOOK_BYPASS_ENV = "CABILDO_GITHOOK_BYPASS"


def cmd_githook(args) -> int:
    """Enforce the git-collision guards at git's own pre-commit/pre-push, so a
    plain `git` in a terminal — a human, or an agent whose git never reaches the
    PreToolUse guard — is judged by the same rules. This is the opt-in,
    daemon-free half of cabildo's coordination; `cabildo guard install` wires it.

    Fail-open at the exit-code boundary: ANY error exits 0, so a broken or slow
    guard never blocks the user's commit/push. Only a clean, intended block exits
    1, with the reason on stderr for git to show.
    """
    try:
        if os.environ.get(GITHOOK_BYPASS_ENV):
            return 0  # the daemon's own push: never self-deny
        command = _GITHOOK_COMMANDS.get(args.event)
        if command is None:
            return 0
        from . import guard

        conn = store.connect()
        try:
            me = guard.resolve_actor(conn)
            reason = guard.deny_git(conn, me, command)
        finally:
            conn.close()
    except Exception:
        return 0  # never break the user's git over a guard error
    if reason:
        print(reason, file=sys.stderr)
        return 1
    return 0


_GUARD_MARK_START = "# >>> cabildo guard >>>"
_GUARD_MARK_END = "# <<< cabildo guard <<<"


def _guard_block(event: str) -> str:
    return f"{_GUARD_MARK_START}\ncabildo githook {event} || exit $?\n{_GUARD_MARK_END}\n"


def _git_hooks_dir(repo: str) -> Path | None:
    """The directory git runs hooks from for ``repo`` — honouring an explicit
    ``core.hooksPath`` (husky/lefthook), else ``<git-dir>/hooks`` (worktree-aware
    via ``--absolute-git-dir``). None if ``repo`` is not a git work tree.
    """

    def _git(*args: str) -> str | None:
        try:
            p = subprocess.run(
                ["git", "-C", repo, *args], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return p.stdout.strip() if p.returncode == 0 else None

    hp = _git("config", "--get", "core.hooksPath")
    if hp:
        return Path(hp) if os.path.isabs(hp) else Path(repo) / hp
    git_dir = _git("rev-parse", "--absolute-git-dir")
    return Path(git_dir) / "hooks" if git_dir else None


def _install_one_hook(hooks_dir: Path, event: str, *, dry_run: bool) -> str:
    """Install (or report) the cabildo block in the ``event`` hook, composing
    with whatever is already there. Returns a one-line status.
    """
    block = _guard_block(event)
    path = hooks_dir / event
    existing = path.read_text() if path.exists() else ""
    if f"cabildo githook {event}" in existing:
        return f"{event}: already installed"
    if not existing:
        new = "#!/usr/bin/env bash\n" + block
    else:
        lines = existing.splitlines(keepends=True)
        if lines[0].startswith("#!"):  # run cabildo first, keep the rest below
            new = lines[0] + block + "".join(lines[1:])
        else:
            new = block + existing
    if dry_run:
        return f"{event}: would write {path}\n{new}"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    path.chmod(path.stat().st_mode | 0o111)  # make it executable
    return f"{event}: installed → {path}"


def cmd_guard_install(args) -> int:
    """Wire the git-hook guard into this repo's pre-commit and pre-push, so plain
    `git` here is coordinated like an agent's tool-call git — without needing the
    MCP server or the daemon. Idempotent, and it chains ahead of any hook you
    already have rather than clobbering it. `--print` shows what it would do.
    """
    repo = os.getcwd()
    hooks_dir = _git_hooks_dir(repo)
    if hooks_dir is None:
        print(f"cabildo: {repo} is not a git work tree — nothing to install", file=sys.stderr)
        return 1
    for event in ("pre-commit", "pre-push"):
        print(_install_one_hook(hooks_dir, event, dry_run=args.print))
    if not args.print:
        print("Bypass any one run with `git commit`/`git push --no-verify`.")
    return 0


def cmd_guard_uninstall(args) -> int:
    """Remove the cabildo block from this repo's pre-commit/pre-push hooks,
    leaving anything else in them untouched.
    """
    repo = os.getcwd()
    hooks_dir = _git_hooks_dir(repo)
    if hooks_dir is None:
        print(f"cabildo: {repo} is not a git work tree", file=sys.stderr)
        return 1
    import re as _re

    pattern = _re.compile(
        _re.escape(_GUARD_MARK_START) + r".*?" + _re.escape(_GUARD_MARK_END) + r"\n?",
        _re.DOTALL,
    )
    for event in ("pre-commit", "pre-push"):
        path = hooks_dir / event
        if not path.exists():
            continue
        text = path.read_text()
        stripped = pattern.sub("", text)
        if stripped != text:
            path.write_text(stripped)
            print(f"{event}: removed cabildo block")
        else:
            print(f"{event}: nothing to remove")
    return 0


def cmd_doctor(args) -> int:
    """One health check across the whole install: the six hooks, both MCP
    servers, the bus DB (reachable + WAL), the daemon, and the platform features
    cabildo needs on THIS machine. Exit non-zero only when something is genuinely
    broken — a WARN (a platform gap, no daemon running) is flagged but passes.
    """
    from . import doctor

    checks = doctor.run()
    for c in checks:
        col, glyph = _DOCTOR_GLYPH.get(c.status, (C.DIM, "?"))
        print(f"  {col}{glyph}{C.OFF} {C.BOLD}{c.name:<17}{C.OFF} {c.detail}")
        if c.fix:
            print(f"      {C.DIM}→ {c.fix}{C.OFF}")

    n_fail = sum(c.status == doctor.FAIL for c in checks)
    n_warn = sum(c.status == doctor.WARN for c in checks)
    if n_fail:
        print(
            f"\n{C.RED}{C.BOLD}{n_fail} problem(s){C.OFF} — fix the {C.RED}✗{C.OFF} line(s) above."
        )
        return 1
    if n_warn:
        print(f"\n{C.GREEN}ok{C.OFF} {C.DIM}— {n_warn} warning(s); nothing broken.{C.OFF}")
        return 0
    print(f"\n{C.GREEN}{C.BOLD}healthy{C.OFF} — everything wired.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # bare `cabildo` and `cabildo workspace ...` open the workspace; textual loads only here
    if not argv or argv[0] == "workspace":
        from cabildo.workspace.app import main as workspace_main

        return workspace_main(argv[1:])
    p = argparse.ArgumentParser(prog="cabildo", description=(__doc__ or "").split("\n")[0])
    # `--version` fires during parsing and exits before the required-subcommand
    # check, so `cabildo --version` works without naming a subcommand.
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="chatroom on a tty; plain tail when piped")
    w.add_argument("-t", "--topic", help="channel your messages go to (tail: filter)")
    w.add_argument("-n", "--tail", type=int, default=50, help="backlog lines first")
    w.add_argument("--interval", type=float, default=1.0, help="poll seconds (tail mode)")
    w.add_argument("--no-input", action="store_true", help="plain tail even on a tty")
    w.add_argument(
        "--snapshot",
        action="store_true",
        help="render one frame of the TUI to plain text and exit (no terminal needed)",
    )
    w.add_argument("--rows", type=int, default=24, help="snapshot height (default: 24)")
    w.add_argument("--cols", type=int, default=100, help="snapshot width (default: 100)")
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

    # Push delivery: the machine-readable feed the bundled mod polls.
    wh = sub.add_parser("whoami", help="the acting session's id + handle (JSON for the mod)")
    wh.add_argument("--json", action="store_true", help="machine-readable output")
    wh.set_defaults(fn=cmd_whoami)

    pl = sub.add_parser("pull", help="inbox messages with id > --after, oldest first (cursor feed)")
    pl.add_argument("--after", type=int, default=0, help="cursor: only messages with a greater id")
    pl.add_argument("--session", help="act as this session id (default: resolve from the pid walk)")
    pl.add_argument("--urgent-only", action="store_true", help="only urgent messages")
    pl.add_argument("--limit", type=int, default=50, help="max rows (default: 50)")
    pl.add_argument(
        "--preview-chars", type=int, default=200, help="body preview length (default: 200)"
    )
    pl.add_argument("--json", action="store_true", help="machine-readable output")
    pl.set_defaults(fn=cmd_pull)

    wt = sub.add_parser(
        "wait", help="block until the inbox has a message with id > --after, then print it (JSON)"
    )
    wt.add_argument("--after", type=int, default=0, help="cursor: only messages with a greater id")
    wt.add_argument("--session", help="act as this session id (default: resolve from the pid walk)")
    wt.add_argument("--urgent-only", action="store_true", help="only urgent messages")
    wt.add_argument("--limit", type=int, default=50, help="max rows (default: 50)")
    wt.add_argument(
        "--preview-chars", type=int, default=200, help="body preview length (default: 200)"
    )
    wt.add_argument(
        "--timeout", type=float, default=300.0, help="exit with no messages after this (s)"
    )
    wt.add_argument("--interval", type=float, default=0.25, help="data_version check interval (s)")
    wt.add_argument("--json", action="store_true", help="accepted for symmetry; output is JSON")
    wt.set_defaults(fn=cmd_wait)

    sd = sub.add_parser("send", help="post as the acting session (unlike `say`, which is @jorge)")
    sd.add_argument("text", nargs="+")
    sd.add_argument("--to", help="direct message: handle, id, or short id")
    sd.add_argument("-t", "--topic", help="channel instead of a DM")
    sd.add_argument("--session", help="act as this session id (default: resolve from the pid walk)")
    sd.add_argument("-u", "--urgent", action="store_true", help="blocks the recipient from idling")
    sd.add_argument("--json", action="store_true", help="machine-readable output")
    sd.set_defaults(fn=cmd_send)

    mr = sub.add_parser("mark-read", help="mark inbox messages read by id")
    mr.add_argument("--ids", help="comma-separated message ids")
    mr.add_argument("--all", action="store_true", help="mark every unread message read")
    mr.add_argument("--session", help="act as this session id (default: resolve from the pid walk)")
    mr.add_argument("--json", action="store_true", help="machine-readable output")
    mr.set_defaults(fn=cmd_mark_read)

    mt = sub.add_parser("meter", help="record a push-triggered event in hook_metrics (auditable)")
    mt.add_argument("--event", default="push_wake", help="event name (default: push_wake)")
    mt.add_argument("--chars", type=int, default=0, help="chars injected into the woken turn")
    mt.add_argument("--session", help="act as this session id (default: resolve from the pid walk)")
    mt.add_argument("--json", action="store_true", help="machine-readable output")
    mt.set_defaults(fn=cmd_meter)

    ins = sub.add_parser(
        "install", help="wire cabildo into Claude Code: MCP server, hooks and the push mod"
    )
    ins.add_argument(
        "--client",
        choices=("claude", "codex", "opencode", "both", "all"),
        default="claude",
        help="which client(s) to wire (default: claude)",
    )
    ins.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show what would be done and change nothing",
    )
    ins.set_defaults(fn=cmd_install)

    pg = sub.add_parser(
        "plugin", help="install the bundled Claude Code mod (push delivery) via `claude plugin`"
    )
    pg.add_argument("--remove", action="store_true", help="uninstall the mod instead of installing")
    pg.add_argument(
        "--scope",
        choices=("user", "project", "local"),
        default="user",
        help="install scope (default: user — every session on this machine)",
    )
    pg.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show the commands and change nothing",
    )
    pg.set_defaults(fn=cmd_plugin)

    se = sub.add_parser(
        "sessions",
        help="your Claude sessions in this project, with titles, to find & resume a lost one",
    )
    se.add_argument("-a", "--all", action="store_true", help="every project, not just this one")
    se.add_argument("-n", "--limit", type=int, default=20, help="max sessions to show")
    se.set_defaults(fn=cmd_sessions)
    se_sub = se.add_subparsers(dest="sessions_cmd")
    for name, help_ in (
        ("list", "sessions of this folder and its subfolders, with tags"),
        ("tag", "add or remove tags"),
        ("read", "print the user and assistant text"),
        ("trash", "move to the workspace trash (needs a session made from them)"),
    ):
        sp = se_sub.add_parser(name, help=help_)
        sp.add_argument("--dir", default=".", help="project folder (default: here)")
        sp.add_argument("--all", action="store_true", help="every folder, not only --dir")
        sp.add_argument("-t", "--tag", help="select by tag")
        if name == "list":
            sp.add_argument("-q", "--query", help="match title, label, first prompt")
            sp.add_argument("-c", "--contains", help="match any user or assistant text")
        else:
            sp.add_argument("ids", nargs="*", help="session ids or 8-char prefixes")
        if name == "tag":
            sp.add_argument("--add", action="append", default=[], help="tag to add")
            sp.add_argument("--remove", action="append", default=[], help="tag to remove")
        if name == "trash":
            sp.add_argument("--force", action="store_true", help="skip the made-from check")
        sp.set_defaults(fn=cmd_session_ops)

    st = sub.add_parser(
        "stop", help="set the fleet's wind-down line (e.g. 'stop 23:00'); bare to show it"
    )
    st.add_argument("time", nargs="?", help="when you mean to stop: 23:00, 11pm, 23")
    st.add_argument(
        "--project", action="store_true", help="scope the line to this repo, not the whole fleet"
    )
    st.add_argument("--clear", action="store_true", help="stand the stop line down")
    st.add_argument(
        "--all", action="store_true", help="with --clear: clear every scope, not just this one"
    )
    st.set_defaults(fn=cmd_stop)

    mm = sub.add_parser("mem", help="this machine's RAM, attributed to the agents holding it")
    mm.add_argument("--loose", action="store_true", help="only what agents left behind")
    mm.add_argument("--min-mb", type=int, default=100, help="ignore processes below this")
    mm.add_argument("--top", type=int, default=12, help="how many processes to list")
    mm.set_defaults(fn=cmd_mem)

    bw = sub.add_parser("browser", help="the browser agents use: status, reap, doctor")
    bw_sub = bw.add_subparsers(dest="browser_cmd", required=True)
    bw_sub.add_parser("status", help="servers, chrome processes, memory, saved login")
    bw_sub.add_parser("doctor", help="check the cabildo-browser entry: isolated, headed")
    bw_login = bw_sub.add_parser(
        "login", help="log into sites in a visible window, save for agents"
    )
    bw_login.add_argument(
        "--profile", help="browser profile directory (default: the one with the logins)"
    )
    bw_login.add_argument(
        "--channel", default="chrome", help="Chrome channel to open (default: chrome)"
    )
    bw_serve = bw_sub.add_parser("serve", help="run the browser MCP server (stdio)")
    bw_serve.add_argument("--channel", default="chrome", help="Chrome channel (default: chrome)")
    bw_serve.add_argument(
        "--browser", help="same as --channel; for configs written for playwright-mcp"
    )
    bw_serve.add_argument("--storage-state", help="saved login file every browser starts from")
    bw_serve.add_argument("--output-dir", help="where screenshots and snapshots are saved")
    bw_serve.add_argument("--isolated", action="store_true", help="accepted; always on")
    bw_serve.add_argument("--headless", action="store_true", help="refused: the browser is visible")
    bw_serve.add_argument("--user-data-dir", help="refused: browsers are isolated")
    bw_reap = bw_sub.add_parser("reap", help="stop browser processes whose session is gone")
    bw_reap.add_argument("--all", action="store_true", help="every server, live sessions too")
    bw_reap.add_argument(
        "--include-mine", action="store_true", help="with --all: also this session's own server"
    )
    bw_reap.add_argument(
        "--dry-run", action="store_true", help="list what would stop, stop nothing"
    )
    bw.set_defaults(fn=cmd_browser)

    co = sub.add_parser(
        "cost", help="what running cabildo costs: injected context, hook latency, tool traffic"
    )
    co.add_argument(
        "-d", "--days", type=float, default=0, help="only the last N days (default: all time)"
    )
    co.add_argument("--session", help="scope to one session id")
    co.add_argument("--rate", type=float, help="€ per 1M tokens, for a rough € estimate")
    co.set_defaults(fn=cmd_cost)

    pr = sub.add_parser(
        "pair",
        help="pair with another machine on the LAN: no PIN prints one, a PIN joins",
    )
    pr.add_argument("pin", nargs="?", help="the 6-digit PIN shown on the other machine")
    pr.add_argument("--host", help="the other machine's address, when mDNS finds nothing")
    pr.add_argument("--port", type=int, default=0, help="the other machine's port, with --host")
    pr.add_argument("--wait", type=float, default=90.0, help="seconds to wait for the daemon")
    pr.set_defaults(fn=cmd_pair)

    wn = sub.add_parser("wan", help="show the WAN peers the daemon syncs with")
    wn.set_defaults(fn=cmd_wan)

    pr = sub.add_parser(
        "project", help="list projects and set their dials: cloud uplink + local peer links"
    )
    pr.add_argument("project", nargs="?", help="project to inspect or configure")
    pr.add_argument(
        "--uplink",
        choices=cloud_uplink_levels(),
        help="how much reaches the cloud (none<presence<messages<content)",
    )
    pr.add_argument(
        "--link", metavar="OTHER", help="let this project and OTHER see each other on the bus"
    )
    pr.add_argument("--unlink", metavar="OTHER", help="undo a --link")
    pr.add_argument(
        "--default",
        choices=cloud_uplink_levels(),
        help="set the default uplink for projects not listed",
    )
    pr.set_defaults(fn=cmd_project)

    ad = sub.add_parser(
        "add", help="put a folder on the bus: pin its project name + wire its .mcp.json"
    )
    ad.add_argument("path", nargs="?", default=".", help="folder (default: cwd)")
    ad.add_argument(
        "-p",
        "--project",
        metavar="NAME",
        help="project name to pin this folder to (default: repo dir name)",
    )
    ad.add_argument(
        "--uplink",
        choices=cloud_uplink_levels(),
        help="how much of it reaches the cloud (default: your default_uplink)",
    )
    ad.add_argument(
        "--link", metavar="OTHER", help="also let this project and OTHER see each other on the bus"
    )
    ad.add_argument(
        "--exact",
        action="store_true",
        help="register the given folder itself, not the git repo containing it",
    )
    ad.add_argument(
        "--no-mcp", action="store_true", help="pin the name only; don't touch .mcp.json"
    )
    ad.add_argument(
        "--remove",
        action="store_true",
        help="undo: unpin the folder and drop cabildo from its .mcp.json",
    )
    ad.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show what would be written, change nothing",
    )
    ad.set_defaults(fn=cmd_add)

    cx = sub.add_parser(
        "codex", help="wire Codex onto the same bus (codex mcp add, or print the config block)"
    )
    cx.add_argument("path", nargs="?", default=".", help="folder to derive the project name from")
    cx.add_argument(
        "-p", "--project", metavar="NAME", help="project name to pin (default: repo dir name)"
    )
    cx.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show what would be done, change nothing",
    )
    cx.add_argument("--remove", action="store_true", help="undo: drop cabildo from Codex's config")
    cx.set_defaults(fn=cmd_codex)

    oc = sub.add_parser(
        "opencode",
        help="wire opencode onto the same bus (edits opencode.json, installs /agenda + /sanity)",
    )
    oc.add_argument("path", nargs="?", default=".", help="folder to derive the project name from")
    oc.add_argument(
        "-p", "--project", metavar="NAME", help="project name to pin (default: repo dir name)"
    )
    oc.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show what would be done, change nothing",
    )
    oc.add_argument(
        "--remove", action="store_true", help="undo: drop cabildo from opencode's config + commands"
    )
    oc.set_defaults(fn=cmd_opencode)

    mn = sub.add_parser(
        "minutes", help="the day's conversation minutas for a project (from transcripts)"
    )
    mn.add_argument("-p", "--project", help="project name (default: this repo)")
    mn.add_argument("--date", help="YYYY-MM-DD (default: today)")
    mn.add_argument(
        "--write",
        action="store_true",
        help="write to <cabildo home>/minutes/<project>/<date>.md instead of printing",
    )
    mn.set_defaults(fn=cmd_minutes)

    mg = sub.add_parser("messages", help="recorded conversation turns from the bus DB")
    mg.add_argument("-p", "--project", help="project name (default: this repo)")
    mg.add_argument("--all", action="store_true", help="all projects, not just this repo")
    mg.add_argument("--session", help="only this session id")
    mg.add_argument("--role", choices=["user", "assistant"], help="only this role")
    mg.add_argument("--grep", help="substring to match in the message body")
    mg.add_argument("--since", type=float, default=0.0, help="only the last N hours")
    mg.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    mg.add_argument(
        "--import",
        dest="import_all",
        action="store_true",
        help="record every transcript on disk and in the workspace trash, then exit",
    )
    mg.set_defaults(fn=cmd_messages)

    nt = sub.add_parser(
        "notes", help="session loose ends (issue #34), added by agents via note_add"
    )
    nt.add_argument(
        "--session", help="whose notes (default: resolve this session like the git hook)"
    )
    nt.add_argument("--all", action="store_true", help="every session's notes, not just one")
    nt.add_argument(
        "--state",
        choices=["open", "done", "parked", ""],
        default="open",
        help="filter by state (default open; '' for all)",
    )
    nt.add_argument(
        "--type",
        choices=list(store.NOTE_TYPES),
        help="filter by type (topic/need/bug/idea/unfinished)",
    )
    nt.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    nt.add_argument(
        "--add", metavar="TEXT", help="write a note for --session with no model call (source=human)"
    )
    nt.add_argument("--done", type=int, metavar="ID", help="mark note ID done and exit")
    nt.add_argument("--park", type=int, metavar="ID", help="mark note ID parked and exit")
    nt.add_argument("--ref", help="with --park: where it went (issue name / agenda item)")
    nt.set_defaults(fn=cmd_notes)

    iss = sub.add_parser(
        "issues", help="this project's docs/issues register: what's ready, what's blocked"
    )
    iss.add_argument(
        "action",
        nargs="?",
        default="ready",
        choices=["ready", "list", "show", "check", "reindex", "init"],
        help="default: ready — what is unblocked and unowned",
    )
    iss.add_argument("name", nargs="?", help="for `show`: a filename or slug fragment")
    iss.add_argument(
        "-C",
        "--folder",
        metavar="DIR",
        default=".",
        help="where to look for docs/issues (default: cwd, searching upward)",
    )
    iss.add_argument("--status", help="filter: open | in-progress | blocked | done")
    iss.add_argument("--owner", help="filter: a handle, or `none` for unowned")
    iss.add_argument(
        "--area",
        action="append",
        metavar="NAME",
        help="filter by area; on `init` for a new folder, declare one (repeatable)",
    )
    iss.add_argument(
        "--force", action="store_true", help="for `init`: overwrite an existing config.toml"
    )
    iss.set_defaults(fn=cmd_issues)

    sv = sub.add_parser("service", help="run the daemon as a systemd --user service")
    sv.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["install", "status", "logs", "start", "stop", "restart", "uninstall"],
        help="default: status",
    )
    sv.add_argument(
        "--allow-push", action="store_true", help="let the daemon actually push (default: dry run)"
    )
    sv.add_argument(
        "--allow-spawn",
        action="store_true",
        help="let the daemon actually launch agents (default: dry run)",
    )
    sv.add_argument("--max-agents", type=int, default=3, help="concurrent spawned agents")
    sv.add_argument("--max-depth", type=int, default=2, help="how deep spawn chains may go")
    sv.add_argument(
        "--budget",
        type=float,
        default=0.0,
        help="weighted-token ceiling over --budget-days (0 = no throttle)",
    )
    sv.add_argument("--budget-days", type=float, default=7.0, help="window for --budget")
    sv.add_argument(
        "--max-intent-age",
        type=float,
        default=2.0,
        help="hours a queued intent may wait before the daemon refuses "
        "it as stale instead of running it (0 disables)",
    )
    sv.add_argument(
        "--no-notify",
        action="store_true",
        help="don't pop desktop notifications for messages addressed to you",
    )
    sv.add_argument(
        "--save-messages",
        action="store_true",
        help="record conversation turns into the bus DB for projects that set "
        "[record] messages = true (text stays local, never uplinked)",
    )
    sv.add_argument(
        "--linger",
        action="store_true",
        help="keep the services running after you log out (loginctl enable-linger)",
    )
    sv.add_argument(
        "--no-start", action="store_true", help="install: enable at login but don't start now"
    )
    sv.add_argument(
        "--force", action="store_true", help="replace (or remove) a unit file cabildo didn't write"
    )
    sv.add_argument("-n", "--lines", type=int, default=40, help="logs: how many lines")
    sv.add_argument("-f", "--follow", action="store_true", help="logs: keep streaming")
    sv.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="install: print the unit files instead of writing them",
    )
    sv.set_defaults(fn=cmd_service)

    vw = sub.add_parser("viewer", help="read-only web view of the local bus on localhost")
    vw.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    vw.add_argument("--port", type=int, default=8787, help="port (default: 8787)")
    vw.add_argument("--open", action="store_true", help="open a browser tab")
    vw.set_defaults(fn=cmd_viewer)

    dr = sub.add_parser(
        "doctor", help="verify the install (hooks, MCP, DB, daemon) and flag platform gaps"
    )
    dr.set_defaults(fn=cmd_doctor)

    gh = sub.add_parser(
        "githook",
        help="enforce the git guards from a git pre-commit/pre-push hook (plain-git users)",
    )
    gh.add_argument("event", choices=sorted(_GITHOOK_COMMANDS))
    gh.set_defaults(fn=cmd_githook)

    gd = sub.add_parser("guard", help="wire the git-hook guard into this repo (opt-in, plain git)")
    gdsub = gd.add_subparsers(dest="guard_cmd", required=True)
    gdi = gdsub.add_parser("install", help="add cabildo to this repo's pre-commit/pre-push hooks")
    gdi.add_argument("--print", action="store_true", help="show what would change, write nothing")
    gdi.set_defaults(fn=cmd_guard_install)
    gdu = gdsub.add_parser("uninstall", help="remove cabildo from this repo's git hooks")
    gdu.set_defaults(fn=cmd_guard_uninstall)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
