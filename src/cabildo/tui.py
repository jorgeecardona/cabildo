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
channel. ↑/↓ recall lines you've already sent (readline-style), distinct from
PgUp/PgDn which scroll the pane. The view always shows every channel and DM on
the bus — this is the operator's console, not a subscriber's.

Stdlib only (curses), like the rest of the package.
"""

from __future__ import annotations

import curses
import locale
import re
import textwrap
import time

from . import store
from .store import HUMAN_HANDLE, HUMAN_SESSION

SIDEBAR_W = 30  # hidden when the terminal is too narrow to afford it
MIN_COLS_FOR_SIDEBAR = 72
POLL_MS = 200  # input timeout doubles as the DB poll interval
ROSTER_EVERY = 2.0  # seconds between roster/claims refreshes
HINT = "@name=DM · /u=urgent · /p <project>=go · /t topic · ↑↓ history · PgUp/PgDn · /q=quit"


def resolve_topic(name: str, existing: set[str]) -> str:
    """Map a typed target to a real channel. A bare project name resolves to
    its #proj:<name> channel when that exists, so `/t paas` (or `/p paas`) just
    works instead of forcing the full `/t proj:paas`. An exact match wins; an
    unknown name is used verbatim (creating a new channel, the old behavior).
    """
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


def completions(token: str, agents: list[str], channels: list[str]) -> list[str]:
    """Candidates for the word being typed: `@<prefix>` matches agent names,
    `#<prefix>` matches channels, prefix-insensitive. `@` or `#` alone lists all.
    Anything else completes nothing. Pure, so it is unit-tested without a terminal.
    """
    if token.startswith("@"):
        pre = token[1:].lower()
        return [f"@{a}" for a in agents if a.lower().startswith(pre)]
    if token.startswith("#"):
        pre = token[1:].lstrip("#").lower()
        return [f"#{c}" for c in channels if c.lower().startswith(pre)]
    return []


def wheel_direction(bstate: int) -> int:
    """+1 for a scroll-wheel-up event, -1 for wheel-down, 0 for anything else.
    Pure, so the wheel-to-offset mapping is unit-tested without a terminal.
    """
    up = getattr(curses, "BUTTON4_PRESSED", 0)
    down = getattr(curses, "BUTTON5_PRESSED", 0)
    if up and bstate & up:
        return 1
    if down and bstate & down:
        return -1
    return 0


def chat_scroll(offset: int, pin: bool, direction: int, step: int) -> tuple[int, bool]:
    """New (offset, pin) after a chat scroll. ``offset`` is rows up from the live
    bottom (0 = following the tail); ``direction`` is +1 for up (older messages)
    and -1 for down (toward live).

    Scrolling up pins the view so a new arrival can't yank it (see hold_offset).
    Scrolling down clears the pin: the user is heading back to live, so arrivals
    are allowed to carry the view there instead of fighting each notch. Without
    this, on a busy channel a single 3-row notch loses to a burst of arrivals and
    the view can never reach the bottom -- the "can't get back to live" bug.

    Pure, so the return-to-live behaviour is unit-tested without a terminal.
    """
    if direction > 0:
        return offset + step, True
    if direction < 0:
        return max(0, offset - step), False
    return offset, pin


def hold_offset(offset: int, pin: bool, rows: int) -> int:
    """New scrollback offset after ``rows`` new rows arrive at the bottom.

    Pinned in scrollback (pin and offset>0), the offset grows with the arrival so
    the same messages stay on screen. Once the user has scrolled down (pin
    cleared) arrivals are free to slide the view toward live. Pure.
    """
    return offset + rows if (pin and offset) else offset


# Presence glyphs for the roster, keyed by store.presence_state.
PRESENCE_DOT = {"working": "●", "idle": "○", "stale": "·", "ended": "×", "unknown": "·"}


def agent_badge(presence: str, spawn: dict | None) -> tuple[str, str]:
    """Roster badge for a session: ``(dot, suffix)``. ``dot`` shows presence;
    ``suffix`` is a sub-agent branch + role (e.g. ``" ⤷qa"``) when the session was
    spawned, empty for a top-level one. Pure, so the sub-agent cue is unit-tested
    without a terminal.
    """
    dot = PRESENCE_DOT.get(presence, "·")
    if not spawn:
        return dot, ""
    role = str(spawn.get("role") or "agent")
    return dot, f" ⤷{role}"


def _color_pair(n: int) -> int:
    """``curses.color_pair(n)``, or 0 when curses is not initialised. The offline
    renderer (see snapshot) computes attributes without a terminal, where
    ``color_pair`` would raise; colour just falls away there.
    """
    try:
        return curses.color_pair(n)
    except curses.error:
        return 0


class InputHistory:
    """Readline-style recall of the lines the human has submitted.

    `record` adds a submitted line; `up`/`down` walk the ring. The live draft is
    stashed on the first `up` and restored when `down` steps past the newest
    entry, so browsing history never loses what you were typing. Pure — no curses,
    so it is unit-tested directly.
    """

    def __init__(self) -> None:
        self.items: list[str] = []
        self.idx: int | None = None  # None = composing live, not browsing
        self.draft = ""

    def record(self, line: str) -> None:
        """Remember a submitted line and return to live composing. Blank lines and
        an immediate repeat of the last entry are not stored (shell behaviour).
        """
        self.idx, self.draft = None, ""
        if line.strip() and (not self.items or self.items[-1] != line):
            self.items.append(line)

    def up(self, current: str) -> str | None:
        """The previous entry to show, or None if there is nothing to recall.
        `current` is the live buffer, stashed on the first step back.
        """
        if not self.items:
            return None
        if self.idx is None:
            self.draft = current
            self.idx = len(self.items) - 1
        else:
            self.idx = max(0, self.idx - 1)
        return self.items[self.idx]

    def down(self) -> str | None:
        """The next entry to show, the stashed draft when stepping past the newest,
        or None when not currently browsing (leave the buffer untouched).
        """
        if self.idx is None:
            return None
        self.idx += 1
        if self.idx >= len(self.items):
            self.idx, draft, self.draft = None, self.draft, ""
            return draft
        return self.items[self.idx]


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
        self.msgs: list[dict] = []  # raw message dicts, oldest first
        self.lines: list[list] = []  # wrapped [(text, attr), ...] segments per row
        self.buf = ""  # input buffer
        self.hist = InputHistory()  # ↑/↓ recall of lines you've sent
        self.topics: list[str] = []  # channel names, for # completion; refreshed with the roster
        self.tab: dict | None = None  # active Tab-completion cycle, or None
        self.offset = 0  # scrollback: rows up from the bottom (0 = following live)
        self.pin = False  # hold the scrollback position against arrivals; see chat_scroll
        self.last_id = 0
        self.roster: list = []
        self.spawns: dict = {}  # session_id -> spawn record, for the sub-agent cues
        self.claims: list = []
        self.roster_at = 0.0
        self.notice: str | None = None  # transient local status (errors, sends)

        self.RED = _color_pair(6)  # real pair set in _enter_curses; 0 headless
        # Capture only the scroll wheel. A bare wheel event otherwise falls
        # through to the terminal, which scrolls its own scrollback (the whole
        # TUI frame) instead of the message pane. Masking just the wheel buttons
        # leaves click and drag to the terminal, so text selection/copy still
        # works; the wheel now moves self.offset (see handle()).
        self._wheel = getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0)

        backlog = self._fetch(0, tail)
        row = self.conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM messages").fetchone()
        self.last_id = backlog[-1]["id"] if backlog else row["m"]
        for m in backlog:
            self._append(m)

    def _enter_curses(self) -> None:
        """Terminal-only setup, kept out of __init__ so the layout can be rendered
        headless (see snapshot). Called once at the start of run().
        """
        curses.curs_set(1)
        curses.use_default_colors()
        for i, c in enumerate(
            (
                curses.COLOR_CYAN,
                curses.COLOR_GREEN,
                curses.COLOR_MAGENTA,
                curses.COLOR_BLUE,
                curses.COLOR_YELLOW,
                curses.COLOR_RED,
            ),
            start=1,
        ):
            curses.init_pair(i, c, -1)
        self.RED = curses.color_pair(6)
        if self._wheel:
            try:
                curses.mousemask(self._wheel)
                curses.mouseinterval(0)
            except curses.error:
                self._wheel = 0
        self.scr.timeout(POLL_MS)

    # ---- data ------------------------------------------------------------- #
    def _fetch(self, after: int, limit: int) -> list[dict]:
        rows = list(
            self.conn.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?",
                (after, limit),
            )
        )
        return [store._msg_dict(r) for r in reversed(rows)]

    def _sender_attr(self, name: str) -> int:
        return _color_pair(1 + sum(name.encode()) % 5) | curses.A_BOLD

    def _pane_width(self) -> int:
        h, w = self.scr.getmaxyx()
        return (w - SIDEBAR_W if w >= MIN_COLS_FOR_SIDEBAR else w) - 1

    def _append(self, m: dict) -> None:
        self.msgs.append(m)
        wrapped = self._wrap(m, self._pane_width())
        self.lines.extend(wrapped)
        # Reading scrollback (pinned): hold the view against arrivals. After a
        # downward scroll the pin is off, so arrivals carry the view toward live.
        self.offset = hold_offset(self.offset, self.pin, len(wrapped))

    def _wrap(self, m: dict, width: int) -> list[list]:
        width = max(20, width)
        who = m["from_display"]
        dest = m["topic"] and f"#{m['topic']}"
        if m["to_session"]:
            s = store.get_session(self.conn, m["to_session"])
            dest = store.display_name(s, m["to_session"])
        clock = time.strftime("%H:%M:%S", time.localtime(m["created_at"]))
        head = [(clock + " ", curses.A_DIM)]
        if self.spawns.get(m["from_session"]):  # a spawned sub-agent
            head.append(("⤷ ", _color_pair(3) | curses.A_BOLD))
        head.append((who, self._sender_attr(who)))
        head.append((" → " + (dest or "?"), curses.A_DIM))
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
        self.spawns = store.spawn_index(self.conn)
        self.claims = store.list_claims(self.conn)
        self.topics = [t["name"] for t in store.list_topics(self.conn)]
        self.roster_at = time.time()

    def _presence_attr(self, presence: str) -> int:
        return {
            "working": _color_pair(2) | curses.A_BOLD,  # green
            "idle": _color_pair(5),  # yellow
        }.get(presence, curses.A_DIM)

    # ---- completion ------------------------------------------------------- #
    def _trailing(self) -> str:
        """The word currently being typed — the run of non-spaces at the end of
        the buffer, which is what `@`/`#` completion acts on.
        """
        m = re.search(r"\S*$", self.buf)
        return m.group(0) if m else ""

    def _agent_names(self) -> list[str]:
        return [store.display_name(r).lstrip("@") for r in self.roster]

    def _completions(self) -> list[str]:
        return completions(self._trailing(), self._agent_names(), self.topics)

    def _tab_complete(self) -> None:
        """Tab: fill the trailing @/# word with the first match, then cycle on each
        further Tab. Any other key ends the cycle (handled in `handle`).
        """
        if self.tab is None:
            cands = self._completions()
            if not cands:
                return
            trailing = self._trailing()
            head = self.buf[: len(self.buf) - len(trailing)]
            self.tab = {"cands": cands, "idx": 0, "head": head}
        else:
            self.tab["idx"] = (self.tab["idx"] + 1) % len(self.tab["cands"])
        self.buf = self.tab["head"] + self.tab["cands"][self.tab["idx"]]

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
        self._put(
            0,
            max(0, w - 20),
            f" {len(self.roster)} agents on bus "[:19],
            curses.A_REVERSE | curses.A_DIM,
        )

        # main pane: the message scroll, bottom-anchored, so `offset` counts rows
        # up from the bottom.
        total = len(self.lines)
        self.offset = max(0, min(self.offset, max(0, total - pane_h)))
        start = max(0, total - pane_h - self.offset)
        for row, segs in enumerate(self.lines[start : start + pane_h], start=1):
            x = 0
            for text, attr in segs:
                if x >= pane_w:
                    break
                self._put(row, x, text[: pane_w - x], attr)
                x += len(text)
        if self.offset:
            tag = f" ↑ {self.offset} rows up — End to follow "
            self._put(1, max(0, pane_w - len(tag)), tag, curses.A_REVERSE)

        # sidebar: a live roster with presence + sub-agent cues.
        if sidebar:
            x0 = w - SIDEBAR_W
            inner = SIDEBAR_W - 1
            for row in range(1, h - 2):
                self._put(row, x0 - 1, "│", curses.A_DIM)

            top_limit = h - 3  # last row the roster may use

            self._put(1, x0, "agents", curses.A_BOLD | curses.A_UNDERLINE)
            row = 2
            for r in self.roster:
                if row >= top_limit:
                    break
                presence = store.presence_state(r)
                spawn = self.spawns.get(r["session_id"])
                dot, suffix = agent_badge(presence, spawn)
                self._put(row, x0, dot, self._presence_attr(presence))
                name = store.display_name(r)
                self._put(row, x0 + 2, name[: inner - 2], self._sender_attr(name))
                nx = x0 + 2 + min(len(name), inner - 2)
                if suffix and nx < x0 + inner:
                    self._put(row, nx, suffix[: x0 + inner - nx], _color_pair(3))
                row += 1
                self._put(row, x0 + 2, (r["project_name"] or "-")[: inner - 2], curses.A_DIM)
                row += 1
            if self.claims and row < top_limit - 1:
                self._put(row + 1, x0, "claims", curses.A_BOLD | curses.A_UNDERLINE)
                row += 2
                for c in self.claims:
                    if row >= top_limit:
                        break
                    self._put(row, x0, c["resource"][:inner], curses.A_DIM)
                    row += 1

        # a completion strip (typing @agent / #channel) takes the hint row; else hint/notice
        comp = self.tab["cands"] if self.tab else self._completions()
        sel = self.tab["idx"] if self.tab else -1
        if comp:
            self._put(h - 2, 0, "⇥ ", curses.A_DIM)
            x = 2
            for i, cand in enumerate(comp[:8]):
                if x + len(cand) >= w - 1:
                    break
                self._put(h - 2, x, cand, curses.A_REVERSE if i == sel else curses.A_DIM)
                x += len(cand)
                self._put(h - 2, x, " · ", curses.A_DIM)
                x += 3
        else:
            self._put(
                h - 2,
                0,
                (self.notice or HINT)[: w - 1],
                (self.RED if self.notice else 0) | curses.A_DIM,
            )
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
        raw = self.buf
        kind, p = parse(self.buf)
        self.buf, self.notice, self.tab = "", None, None
        self.hist.record(raw)
        if kind == "quit":
            return False
        if kind == "noop":
            return True
        if kind == "projects":
            projs = sorted(
                t["name"][len("proj:") :]
                for t in store.list_topics(self.conn)
                if t["name"].startswith("proj:")
            )
            self.notice = (
                "projects (use /p <name>): " + ", ".join(projs)
                if projs
                else "no project channels yet"
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
            if ch == "\t":  # Tab: complete / cycle the @/# word
                self._tab_complete()
                return True
            self.tab = None  # any other key ends a completion cycle
            if ch in ("\x7f", "\b"):
                self.buf = self.buf[:-1]
            elif ch == "\x15":  # ctrl-u
                self.buf = ""
            elif ch == "\x04" and not self.buf:  # ctrl-d on empty line
                return False
            elif ch.isprintable():
                self.buf += ch
            return True
        if ch == curses.KEY_BACKSPACE:
            self.buf = self.buf[:-1]
        elif ch == curses.KEY_UP:
            recalled = self.hist.up(self.buf)
            if recalled is not None:
                self.buf = recalled
        elif ch == curses.KEY_DOWN:
            recalled = self.hist.down()
            if recalled is not None:
                self.buf = recalled
        elif ch in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = max(1, (self.scr.getmaxyx()[0] - 3) // 2)
            up = ch == curses.KEY_PPAGE
            self.offset, self.pin = chat_scroll(self.offset, self.pin, 1 if up else -1, step)
        elif ch == curses.KEY_END:
            self.offset = 0
            self.pin = False
        elif ch == curses.KEY_MOUSE:
            self._wheel_scroll()
        elif ch == curses.KEY_RESIZE:
            self._rewrap()
        return True

    def _wheel_scroll(self) -> None:
        try:
            _id, _x, _y, _z, bstate = curses.getmouse()
        except curses.error:
            return
        d = wheel_direction(bstate)
        if not d:
            return
        step = 3
        self.offset, self.pin = chat_scroll(self.offset, self.pin, d, step)

    def run(self) -> int:
        self._enter_curses()
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
            except curses.error:  # timeout: no key this tick
                continue
            if not self.handle(ch):
                return 0


def main(*, topic: str | None = None, tail: int = 50) -> int:
    locale.setlocale(locale.LC_ALL, "")
    try:
        return curses.wrapper(lambda scr: Chat(scr, topic=topic, tail=tail).run())
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------- #
# Offline rendering: draw one frame to plain text, no terminal.
# --------------------------------------------------------------------------- #
class TextScreen:
    """A minimal offline stand-in for a curses window: records ``addstr`` into a
    character grid so ``Chat.draw`` can render to plain text without a terminal.
    Attributes (colour/bold) are dropped; only geometry and characters matter.
    """

    def __init__(self, h: int, w: int) -> None:
        self._h = h
        self._w = w
        self._grid = [[" "] * w for _ in range(h)]

    def getmaxyx(self) -> tuple[int, int]:
        return self._h, self._w

    def erase(self) -> None:
        self._grid = [[" "] * self._w for _ in range(self._h)]

    def move(self, y: int, x: int) -> None:
        # Cursor positioning has no meaning for a static text render.
        _ = (y, x)

    def refresh(self) -> None:
        # No device to flush to; the grid is read directly by render().
        return

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if not (0 <= y < self._h):
            raise curses.error
        row = self._grid[y]
        for i, ch in enumerate(text):
            col = x + i
            if not (0 <= col < self._w):
                raise curses.error  # off the edge; _put swallows it, keeping what fit
            row[col] = ch

    def render(self) -> str:
        return "\n".join("".join(r).rstrip() for r in self._grid)


def snapshot(*, topic: str | None = None, tail: int = 50, size: tuple[int, int] = (24, 100)) -> str:
    """Render one frame of the bus to plain text at ``size`` = (rows, cols), with
    no terminal. Builds a headless Chat against the live DB (honouring
    CABILDO_HOME) and draws it once -- for previewing and snapshot-testing the
    layout.
    """
    h, w = size
    scr = TextScreen(h, w)
    chat = Chat(scr, topic=topic, tail=tail)
    chat._refresh_roster()
    chat.draw()
    return scr.render()
