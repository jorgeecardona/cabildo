"""Full-screen console chatroom for the bus: the human sits IN the room.

`python -m cabildo.cli watch` opens this when stdout is a terminal. Layout:

    ┌ header: what you're viewing, where your words go ──┬─ agents ──┐
    │ message scroll (wrapped, colored, PgUp/PgDn)       │ roster    │
    │                                                    │ claims    │
    ├────────────────────────────────────────────────────┴───────────┤
    │ hint line                                                      │
    │ #general ❯ your message                                        │
    └─────────────────────────────────────────────────────────────────┘

Typing posts as @jorge. `@name hello` DMs an agent; `/u ...` sends urgent
(agents with the Stop hook cannot idle past it); `/p <project>` jumps to that
project's channel (`/p` alone lists them), and `/t topic` retargets to any
channel. The view always shows every channel and DM on the bus — this is the
operator's console, not a subscriber's.

Stdlib only (curses), like the rest of the package.
"""

from __future__ import annotations

import curses
import locale
import textwrap
import time

from . import store
from .store import HUMAN_HANDLE, HUMAN_SESSION

SIDEBAR_W = 24          # hidden when the terminal is too narrow to afford it
MIN_COLS_FOR_SIDEBAR = 72
POLL_MS = 200           # input timeout doubles as the DB poll interval
ROSTER_EVERY = 2.0      # seconds between roster/claims refreshes
HINT = "@name=DM · /u=urgent · /p <project>=go · /t topic · PgUp/PgDn · /q=quit"


def resolve_topic(name: str, existing: set[str]) -> str:
    """Map a typed target to a real channel. A bare project name resolves to
    its #proj:<name> channel when that exists, so `/t paas` (or `/p paas`) just
    works instead of forcing the full `/t proj:paas`. An exact match wins; an
    unknown name is used verbatim (creating a new channel, the old behavior)."""
    name = name.lstrip("#")
    if name in existing:
        return name
    proj = store.project_topic(name)
    return proj if proj in existing else name


# --------------------------------------------------------------------------- #
# Line parsing (pure, testable without a terminal)
# --------------------------------------------------------------------------- #
def parse(line: str) -> tuple[str, dict]:
    """What should happen to a submitted input line.

    Returns (kind, payload): 'quit', 'noop', 'switch' {topic},
    'send' {body, urgent, to} — where to is None (current topic) or a name
    still to be resolved against the bus.
    """
    line = line.strip()
    if not line:
        return "noop", {}
    if line in ("/q", "/quit", "/exit"):
        return "quit", {}
    if line == "/t" or line == "/topic" or line.startswith("/t ") or line.startswith("/topic "):
        parts = line.split(None, 1)
        topic = parts[1].strip().lstrip("#") if len(parts) > 1 else ""
        return ("switch", {"topic": topic}) if topic else ("noop", {})
    # `/p` = jump to a project channel by bare name; `/p` alone lists them.
    if line == "/p" or line == "/proj":
        return "projects", {}
    if line.startswith("/p ") or line.startswith("/proj "):
        parts = line.split(None, 1)
        name = parts[1].strip().lstrip("#") if len(parts) > 1 else ""
        return ("switch", {"topic": name}) if name else ("projects", {})
    urgent = False
    if line == "/u":
        return "noop", {}
    if line.startswith("/u "):
        urgent, line = True, line[3:].strip()
        if not line:
            return "noop", {}
    if line.startswith("@"):
        who, _, rest = line.partition(" ")
        rest = rest.strip()
        if not rest:
            return "noop", {}
        return "send", {"body": rest, "urgent": urgent, "to": who.lstrip("@")}
    return "send", {"body": line, "urgent": urgent, "to": None}


# --------------------------------------------------------------------------- #
# The app
# --------------------------------------------------------------------------- #
class Chat:
    def __init__(self, stdscr, *, topic: str | None, tail: int):
        self.scr = stdscr
        self.conn = store.connect()
        from .cli import ensure_human  # late import: cli imports us too
        ensure_human(self.conn)

        self.dest = (topic or store.DEFAULT_TOPIC).lstrip("#")  # where plain lines go
        self.msgs: list[dict] = []       # raw message dicts, oldest first
        self.lines: list[list] = []      # wrapped [(text, attr), ...] segments per row
        self.buf = ""                    # input buffer
        self.offset = 0                  # scrollback: rows up from the bottom
        self.last_id = 0
        self.roster: list = []
        self.claims: list = []
        self.roster_at = 0.0
        self.notice: str | None = None   # transient local status (errors, sends)

        curses.curs_set(1)
        curses.use_default_colors()
        for i, c in enumerate(
            (curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_MAGENTA,
             curses.COLOR_BLUE, curses.COLOR_YELLOW, curses.COLOR_RED), start=1,
        ):
            curses.init_pair(i, c, -1)
        self.RED = curses.color_pair(6)
        stdscr.timeout(POLL_MS)

        backlog = self._fetch(0, tail)
        row = self.conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM messages").fetchone()
        self.last_id = backlog[-1]["id"] if backlog else row["m"]
        for m in backlog:
            self._append(m)

    # ---- data ------------------------------------------------------------- #
    def _fetch(self, after: int, limit: int) -> list[dict]:
        rows = list(self.conn.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?",
            (after, limit),
        ))
        return [store._msg_dict(r) for r in reversed(rows)]

    def _sender_attr(self, name: str) -> int:
        return curses.color_pair(1 + sum(name.encode()) % 5) | curses.A_BOLD

    def _pane_width(self) -> int:
        h, w = self.scr.getmaxyx()
        return (w - SIDEBAR_W if w >= MIN_COLS_FOR_SIDEBAR else w) - 1

    def _append(self, m: dict) -> None:
        self.msgs.append(m)
        self.lines.extend(self._wrap(m, self._pane_width()))
        if self.offset:
            # Reading scrollback: don't let new arrivals yank the view.
            self.offset += len(self._wrap(m, self._pane_width()))

    def _wrap(self, m: dict, width: int) -> list[list]:
        width = max(20, width)
        who = m["from_display"]
        dest = m["topic"] and f"#{m['topic']}"
        if m["to_session"]:
            s = store.get_session(self.conn, m["to_session"])
            dest = store.display_name(s, m["to_session"])
        clock = time.strftime("%H:%M:%S", time.localtime(m["created_at"]))
        head = [(clock + " ", curses.A_DIM), (who, self._sender_attr(who)),
                (" → " + (dest or "?"), curses.A_DIM)]
        if m["urgent"]:
            head.append((" ⚠ URGENT", self.RED | curses.A_BOLD))
        out = [head]
        body_attr = self.RED if m["urgent"] else curses.A_NORMAL
        for para in m["body"].splitlines() or [""]:
            wrapped = textwrap.wrap(para, width - 2) or [""]
            out.extend([[("  " + ln, body_attr)] for ln in wrapped])
        out.append([("", 0)])  # blank spacer row
        return out

    def _rewrap(self) -> None:
        self.lines = []
        for m in self.msgs:
            self.lines.extend(self._wrap(m, self._pane_width()))

    def _refresh_roster(self) -> None:
        self.roster = store.active_sessions(self.conn)
        self.claims = store.list_claims(self.conn)
        self.roster_at = time.time()

    # ---- drawing ---------------------------------------------------------- #
    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass  # writing the last cell of the last row raises; harmless

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        sidebar = w >= MIN_COLS_FOR_SIDEBAR
        pane_w = self._pane_width()
        pane_h = h - 3  # header + hint + input

        # header
        title = f" cabildo · viewing all · you talk to #{self.dest} "
        self._put(0, 0, title[: w - 1], curses.A_REVERSE)
        self._put(0, max(0, w - 20), f" {len(self.roster)} agents on bus "[: 19],
                  curses.A_REVERSE | curses.A_DIM)

        # messages
        total = len(self.lines)
        self.offset = max(0, min(self.offset, total - pane_h)) if total > pane_h else 0
        start = max(0, total - pane_h - self.offset)
        for row, segs in enumerate(self.lines[start: start + pane_h], start=1):
            x = 0
            for text, attr in segs:
                if x >= pane_w:
                    break
                self._put(row, x, text[: pane_w - x], attr)
                x += len(text)
        if self.offset:
            tag = f" ↑ {self.offset} rows up — End to follow "
            self._put(1, max(0, pane_w - len(tag)), tag, curses.A_REVERSE)

        # sidebar
        if sidebar:
            x0 = w - SIDEBAR_W
            for row in range(1, h - 2):
                self._put(row, x0 - 1, "│", curses.A_DIM)
            self._put(1, x0, "agents", curses.A_BOLD)
            row = 2
            for r in self.roster:
                if row >= h - 3:
                    break
                name = store.display_name(r)
                self._put(row, x0, name[: SIDEBAR_W - 1], self._sender_attr(name))
                row += 1
                self._put(row, x0, f" {r['project_name'] or '-'}"[: SIDEBAR_W - 1],
                          curses.A_DIM)
                row += 1
            if self.claims and row < h - 4:
                self._put(row + 1, x0, "claims", curses.A_BOLD)
                row += 2
                for c in self.claims:
                    if row >= h - 3:
                        break
                    self._put(row, x0, c["resource"][: SIDEBAR_W - 1], curses.A_DIM)
                    row += 1

        # hint / notice, then input
        self._put(h - 2, 0, (self.notice or HINT)[: w - 1],
                  (self.RED if self.notice else 0) | curses.A_DIM)
        prompt = f"#{self.dest} ❯ "
        avail = w - len(prompt) - 2
        shown = self.buf[-avail:] if avail > 0 else ""
        self._put(h - 1, 0, prompt, curses.A_BOLD)
        self._put(h - 1, len(prompt), shown)
        self.scr.move(h - 1, min(w - 1, len(prompt) + len(shown)))
        self.scr.refresh()

    # ---- actions ---------------------------------------------------------- #
    def submit(self) -> bool:
        """Returns False when the app should exit."""
        kind, p = parse(self.buf)
        self.buf, self.notice = "", None
        if kind == "quit":
            return False
        if kind == "noop":
            return True
        if kind == "projects":
            projs = sorted(
                t["name"][len("proj:"):]
                for t in store.list_topics(self.conn)
                if t["name"].startswith("proj:")
            )
            self.notice = (
                "projects (use /p <name>): " + ", ".join(projs)
                if projs else "no project channels yet"
            )
            return True
        if kind == "switch":
            existing = {t["name"] for t in store.list_topics(self.conn)}
            self.dest = resolve_topic(p["topic"], existing)
            store.ensure_topic(self.conn, self.dest, created_by=HUMAN_SESSION)
            return True
        to_session = None
        if p["to"]:
            from .cli import _resolve
            to_session = _resolve(self.conn, p["to"])
            if to_session is None:
                self.notice = f"no agent matches '@{p['to']}' — see sidebar"
                return True
        store.post_message(
            self.conn,
            from_session=HUMAN_SESSION,
            from_handle=HUMAN_HANDLE,
            body=p["body"],
            topic=None if to_session else self.dest,
            to_session=to_session,
            urgent=p["urgent"],
        )
        return True

    def handle(self, ch) -> bool:
        if isinstance(ch, str):
            if ch in ("\n", "\r"):
                return self.submit()
            if ch in ("\x7f", "\b"):
                self.buf = self.buf[:-1]
            elif ch == "\x15":            # ctrl-u
                self.buf = ""
            elif ch == "\x04" and not self.buf:   # ctrl-d on empty line
                return False
            elif ch.isprintable():
                self.buf += ch
            return True
        if ch == curses.KEY_BACKSPACE:
            self.buf = self.buf[:-1]
        elif ch == curses.KEY_PPAGE:
            self.offset += max(1, (self.scr.getmaxyx()[0] - 3) // 2)
        elif ch == curses.KEY_NPAGE:
            self.offset = max(0, self.offset - max(1, (self.scr.getmaxyx()[0] - 3) // 2))
        elif ch == curses.KEY_END:
            self.offset = 0
        elif ch == curses.KEY_RESIZE:
            self._rewrap()
        return True

    def run(self) -> int:
        self._refresh_roster()
        while True:
            for m in self._fetch(self.last_id, 200):
                self.last_id = m["id"]
                self._append(m)
            if time.time() - self.roster_at > ROSTER_EVERY:
                self._refresh_roster()
            self.draw()
            try:
                ch = self.scr.get_wch()
            except curses.error:      # timeout: no key this tick
                continue
            if not self.handle(ch):
                return 0


def main(*, topic: str | None = None, tail: int = 50) -> int:
    locale.setlocale(locale.LC_ALL, "")
    try:
        return curses.wrapper(lambda scr: Chat(scr, topic=topic, tail=tail).run())
    except KeyboardInterrupt:
        return 0
