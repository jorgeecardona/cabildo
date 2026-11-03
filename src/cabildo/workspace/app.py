"""The cabildo workspace — Claude Code sessions and the cabildo bus in one terminal.

Left: the Claude session you work in, running inside this app. Right: the
conversations of one working directory (`a`: with its subfolders, then every directory). Every Claude
you open keeps running while hidden, so switching back is instant. `q` in the
list closes them all; the next start (also after a crash) has them open again.

Run:   cabildo [working-dir]      (default: current directory)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import ContentSwitcher, DataTable, Input, Label, Static
from textual.worker import get_current_worker

from cabildo import __version__, cloud, names, sessions_ops, store
from cabildo.workspace import (
    agents,
    intents,
    keys,
    launch,
    legacy,
    notes,
    plan,
    rooms,
    sessions,
    state,
    usage,
)
from cabildo.workspace.agents import AgentState
from cabildo.workspace.hosting import ClaudePane, Target
from cabildo.workspace.launch import Config
from cabildo.workspace.plan import PlanUsage
from cabildo.workspace.sessions import Filter, Meta, Row, Tokens
from cabildo.workspace.state import OpenState
from cabildo.workspace.usage import Use, fmt_count, fmt_cpu, fmt_mb
from cabildo.workspace.xdgdirs import APP, make_dirs, state_home

CLAUDE = "claude"  # the CLI the panes run; tests point this at a fake

# The list is as wide as its content, between MIN_WIDTH and half the window.
MIN_WIDTH = 36  # the text column keeps 14 cells at the minimum
MARK_WIDTH = 2  # "⏳" takes two cells; reserved so a new mark never widens the table
USE_WIDTH = 4  # CPU over memory: "12%" / "316M", "100%" / "1.2G"
BORDER = 1  # the list's left border
CHROME = MARK_WIDTH + USE_WIDTH + 4 * 2 + 2 + 2 + BORDER  # cell padding of 4 columns, padding,
# scrollbar
PLACEHOLDER = "placeholder"
POLL_SECONDS = 10  # `claude agents` costs ~0.4 s CPU and ~170 MB per call; also run on events
SHARE_SECONDS = 5  # a timed poll reuses another workspace's snapshot up to this old
NEW_POLL_SECONDS = 3  # a new Claude shows in `claude agents` about 2 s after it starts
# CPU / wakeups / memory of each session (usage.py), sampled only while someone can see it
SAMPLE_SECONDS = 10  # while the list shows
TOP_SAMPLE_SECONDS = 2  # while the top view (T) is open
TOP_FIRST_SECONDS = 0.5  # its first numbers: a second sample soon after opening it
TOP_PROCS = 8  # processes shown per session in the top view


@dataclass(frozen=True)
class LiveState:
    """One `claude agents` snapshot, used to mark the rows."""

    agents: dict[str, AgentState] = field(default_factory=dict)
    loaded: bool = False  # False until the first poll returns
    taken_at: float = 0.0  # time.monotonic() when the poll started


def short_path(path: str, width: int) -> str:
    """~-relative, cut from the left so the end of the path stays visible."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + "/"):
        path = "~" + path[len(home) :]
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1) :] if width > 1 else "…"[: max(width, 0)]


def fmt_window(n: int) -> str:
    """``1M``, ``200k``: a window size without a decimal."""
    return f"{n // 1_000_000}M" if n % 1_000_000 == 0 else fmt_count(n)


def grid(label: str, a: str, b: str, tail: str = "") -> str:
    """One mode-line row: a label, then two right-aligned columns, so the numbers of the
    context, tokens, and CPU and memory rows line up under each other.
    """
    return f"{label:<11}{a:>10}{b:>10}{tail}"


def fmt_context(t: Tokens) -> str:
    """``opus 5.5     ctx 299k    of 1M  30%``; empty before the first reply.
    The window is left out for a model whose window is not known.
    """
    if not (t.context or t.output):
        return ""
    window = sessions.context_window(t.model)
    name = sessions.model_name(t.model) or "context"  # "" when the line had no model
    used = f"ctx {fmt_count(t.context)}"
    if not window:
        return grid(name, used, "")
    return grid(name, used, f"of {fmt_window(window)}", f"{100 * t.context / window:>4.0f}%")


def context_style(t: Tokens) -> str:
    """The context line's style once the window is nearly full: amber from 70%, red from
    90%; empty below, or when the window is not known.
    """
    window = sessions.context_window(t.model)
    if not window:
        return ""
    full = t.context / window
    return "bold red" if full >= 0.9 else "bold dark_orange3" if full >= 0.7 else ""


def fmt_tokens(t: Tokens, width: int = 1000) -> str:
    """``tokens       in 1.2M  out 87k  cache 32.4M``: summed over every reply; empty
    before the first. ``in`` leaves out the cache reads, shown apart when ``width`` has
    room for them.
    """
    if not (t.context or t.output):
        return ""
    fresh, out = f"in {fmt_count(t.input - t.cached)}", f"out {fmt_count(t.output)}"
    line = grid("tokens", fresh, out, f"  cache {fmt_count(t.cached)}" if t.cached else "")
    return line if len(line) <= width else grid("tokens", fresh, out)


def fmt_effort(t: Tokens) -> str:
    """``effort       high``: the reasoning effort of the last reply; empty when not known."""
    return grid("effort", t.effort, "") if t.effort else ""


def fmt_use(label: str, use: Use | None, first_mb: float | None = None) -> str:
    """One row of the mode line's CPU and memory table; ``-`` when nothing runs. With only
    a first sample (``first_mb``), CPU is ``…``: it needs two samples, memory needs one.
    """
    if use:
        return grid(label, fmt_cpu(use.cpu), fmt_mb(use.mem_mb))
    if first_mb is not None:
        return grid(label, "…", fmt_mb(first_mb))
    return grid(label, "-", "-")


USE_HEADER = grid("", "cpu", "mem")
TAG_STYLE = "bold black on dark_cyan"
Scope = Literal["here", "subfolders", "all"]
SCOPES: tuple[Scope, ...] = ("here", "subfolders", "all")  # the order `a` goes through
SCOPE_NAMES = {"here": "{proj}", "subfolders": "{proj}/…", "all": "all directories"}
SCOPE_HEADERS = {"here": "Session", "subfolders": "Session · subfolders", "all": "Session · all"}
# the plan usage is fetched once per minute of the clock; every workspace reads the shared
# sample on the same wall-clock beats (:00, :15, :30, :45), so they all change at :00
PLAN_SECONDS = 60.0
PLAN_POLL_SECONDS = 15.0


def fmt_plan(p: PlanUsage, pace: plan.PlanPace | None = None) -> list[str]:
    """The plan table, one row per limit, under a header::

        max plan        used    pace  resets  ETA
        5h               23%  5.9%/h  18:59
        7d               53%  0.9%/h  Sat     Thu 00:46

    ``pace`` is percent per hour, smoothed; ``ETA`` is when the limit reaches 100% at
    that pace, shown only when that is before the reset. Times are local; a 7-day time
    carries its weekday.
    """
    rows = [grid(f"{p.plan} plan".strip(), "used", "pace", f"  {'resets':<8}ETA")]
    for name, lim, at in (
        ("5h", p.five_hour, pace.five_hour if pace else None),
        ("7d", p.seven_day, pace.seven_day if pace else None),
    ):
        if not lim:
            continue
        day = "%a" if name == "7d" else ""
        reset = (
            time.strftime(day or "%H:%M", time.localtime(lim.resets_at)) if lim.resets_at else ""
        )
        full = at.full_at() if at else None
        when = time.strftime(f"{day} %H:%M".strip(), time.localtime(full)) if full else ""
        rate = (f"{at.rate:.1f}%/h" if at.ready else "-") if at else ""
        rows.append(grid(name, f"{lim.percent:.0f}%", rate, f"  {reset:<8}{when}").rstrip())
    return rows


INTENT_LINES = (
    7  # the intents group never takes more rows than this: the screen height is the limit
)
INTENT_POLL_SECONDS = 3.0
INTENT_FAILED = ("failed", "rejected", "expired")


def fmt_age(seconds: float) -> str:
    """``45s``, ``3m``, ``2h`` or ``4d``: one unit, the largest that is not zero."""
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def fmt_intents(items: tuple[intents.Intent, ...], now: float) -> list[tuple[str, str]]:
    """The intents group, as (line, style) pairs::

        intents   state   age
        push cabildo   running    2m
        push timbre    pending    1m
        push cabildo   failed    12m
          working tree has uncommitted tracked changes

    The queue first, then what finished lately. A failure shows its reason on the next row, in
    red. Beyond ``INTENT_LINES`` rows the rest is counted (``+2 more``).
    """
    if not items:
        return [("intents    none in the last hour", "")]
    out: list[tuple[str, str]] = [(grid("intents", "state", "age"), "")]
    shown = 0
    for it in items:
        failed = it.status in INTENT_FAILED
        rows = [
            (
                f"{it.action[:5]:<5}{it.project[:12]:<13}{it.status:<9}{fmt_age(now - it.at):>4}",
                "bold red" if failed else "",
            )
        ]
        if failed and it.reason:
            rows.append((f"  {it.reason}", "red"))
        if len(out) + len(rows) > INTENT_LINES + 1:
            break
        out.extend(rows)
        shown += 1
    if shown < len(items):
        out.append((f"+{len(items) - shown} more", "dim"))
    return out


def plan_style(p: PlanUsage) -> str:
    """Amber from 70% of either limit, red from 90%; like the context line."""
    worst = max((lim.percent for lim in (p.five_hour, p.seven_day) if lim), default=0.0)
    return "bold red" if worst >= 90 else "bold dark_orange3" if worst >= 70 else ""


def unquote(word: str) -> str:
    return word.replace("'", "").replace('"', "")


def last_word(value: str) -> tuple[str, str]:
    """`value` cut before its last word, and that word without quotes; a space inside
    quotes does not cut (`g:'my team'` is one word). After a space the word is "".
    """
    start, quote = 0, ""
    for i, c in enumerate(value):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == " ":
            start = i + 1
    return value[:start], unquote(value[start:])


class EditScreen(ModalScreen[str | None]):
    """Emacs-style minibuffer: one `Prompt: value` line at the bottom. With `choices`,
    a line above it lists them (filtered by what is typed, prefix matches first); the
    first one also shows inline, and Tab takes it. With `words`, only the last word is
    matched and completed (a value of several words, like a filter).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+g", "cancel", "Cancel"),
        Binding("tab", "complete", "Complete", show=False),
    ]

    def __init__(
        self, prompt: str, current: str, choices: tuple[str, ...] = (), *, words: bool = False
    ) -> None:
        super().__init__()
        self._prompt = prompt
        self._current = current
        self._choices = choices
        self._words = words

    def compose(self) -> ComposeResult:
        whole = self._choices and not self._words  # the suggester matches the whole value
        suggester = SuggestFromList(self._choices, case_sensitive=False) if whole else None
        with Vertical(id="mb-area"):
            if self._choices:
                yield Static(id="mb-choices")
            with Horizontal(id="minibuffer"):
                yield Label(f"{self._prompt} ", id="mb-prompt")
                yield Input(value=self._current, suggester=suggester, id="edit-input")

    def matches(self, typed: str) -> list[str]:
        """The choices for `typed` (with `words`: for its last word), prefix matches first."""
        if self._words:
            typed = last_word(typed)[1]
        elif typed == self._current:
            return list(self._choices)  # nothing typed yet
        t = typed.strip().lower()

        def plain(c: str) -> str:
            return unquote(c).lower() if self._words else c.lower()

        prefix = [c for c in self._choices if plain(c).startswith(t)]
        return prefix + [c for c in self._choices if t in plain(c) and c not in prefix]

    def _show_matches(self, typed: str) -> None:
        found = self.matches(typed)
        text = Text("   ".join(found)) if found else Text("(no match: a new one)", style="italic")
        self.query_one("#mb-choices", Static).update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._choices:
            self._show_matches(event.value)

    def action_complete(self) -> None:
        field = self.query_one(Input)
        if self._words:
            head, word = last_word(field.value)
            found = self.matches(field.value) if word else []  # after a space: nothing to do
            if found:
                field.value = head + found[0] + " "
        else:
            found = self.matches(field.value)
            if found:
                field.value = found[0]
        field.action_end()

    def on_mount(self) -> None:
        if self._choices:  # every choice, before anything is typed
            text = Text("   ".join(self._choices))
            self.query_one("#mb-choices", Static).update(text)
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


def keys_help(globals_: keys.GlobalKeys) -> str:
    """The ``?`` screen, with the global keys this app has bound."""
    return KEYS.format(
        version=__version__,
        show=globals_.help("show_list").ljust(12),
        toggle=globals_.help("toggle_list").ljust(12),
    )


KEYS = """\
 workspace {version}

 Anywhere (also inside Claude)
   {show} show the list and focus it
   {toggle} hide / show the list

 In the list
   RET          open the conversation (the first time: start its Claude)
   C-n          new conversation in this directory
   C-r          restart: its Claude ends and starts again on the conversation
   C-d          delete (asks yes or no)
   x            close: its Claude ends, the conversation stays
   l  g  t      label / group / tags  (t on a selection: a, b adds; -c removes)
   SPC          select / unselect; t and D act on the selection, else on the filtered list
   D            trash them, once a session made from them (sessions_create) has started
   n            notes for this session, separated by ; (written with no model call)
   a            list this directory / with subfolders / all directories
   f            filter by group / tag  (g:work t:urgent; empty = all)
   Tab          back to Claude
   j k          move (also the arrows)
   T            CPU, wakeups and memory of each session (like top)
   q  C-q       quit (asks while a Claude runs)
   R            reload: quit and start again on the installed code
   ?            these keys

 Inside Claude every other key is Claude's.        any key closes this
"""


class KeysScreen(ModalScreen[None]):
    """`?`: the app's keys, emacs describe-bindings style. Any key closes it."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Static(self.text, id="keys")

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss(None)


class TopScreen(ModalScreen[None]):
    """`T`: CPU, wakeups and memory of every running session and of its processes, like
    top. The app refreshes it every TOP_SAMPLE_SECONDS while it is open.
    """

    BINDINGS = [Binding("escape,q,T", "close", "Close")]

    def __init__(self, lines: list[tuple[Text, str, str, str]]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            yield Static(
                "Every Claude on this machine · CPU: % of one core · Wake/s: times "
                "its threads ran (the power cost) · Mem: anonymous resident memory · "
                "q closes",
                id="top-title",
            )
            yield DataTable(id="top-table", cursor_type="row", zebra_stripes=False)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Session / process", width=48)
        for label in ("CPU", "Wake/s", "Mem"):
            table.add_column(label, width=7)
        self.show(self._lines)
        table.focus()

    def show(self, lines: list[tuple[Text, str, str, str]]) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        for line in lines:
            table.add_row(*line)
        if lines:
            table.move_cursor(row=min(row, len(lines) - 1))

    def action_close(self) -> None:
        self.dismiss(None)


class SwitcherApp(App[None]):
    """The list on the right, the hosted Claude sessions on the left."""

    ENABLE_COMMAND_PALETTE = False  # its C-p would open a palette over the list

    CSS = """
    Screen { background: $background; }
    #body { height: 1fr; }
    #main { width: 1fr; height: 1fr; }
    #main > * { width: 1fr; height: 1fr; }
    #placeholder { color: $text-muted; padding: 1 2; }
    #side { height: 1fr; border-left: solid $panel; }
    #side:focus-within { border-left: solid $accent; }
    DataTable {
        height: 1fr;
        background: $background;
        color: $foreground;
        border: none;
        padding: 0 1;
    }
    DataTable > .datatable--header {
        text-style: bold;
        color: $accent;
        background: $background;
    }
    DataTable > .datatable--cursor {          /* list not focused: Claude has the keys */
        background: $panel;
        color: $foreground;
    }
    DataTable:focus > .datatable--cursor {
        background: $accent;
        color: $background;
        text-style: bold;
    }
    #modeline {
        dock: bottom;
        height: auto;  /* groups of lines, a rule between each */
        background: $foreground;
        color: $background;
        padding: 0 1;
    }
    EditScreen { background: transparent; }
    KeysScreen { align: center middle; background: transparent; }
    TopScreen { align: center middle; background: transparent; }
    #top { width: 80%; height: 80%; border: round $accent; background: $panel; padding: 0 1; }
    #top-title { color: $text-muted; height: 1; }
    #top-table { height: 1fr; background: $panel; }
    #keys { width: auto; height: auto; padding: 1 2; border: round $accent; background: $panel; }
    #mb-area { dock: bottom; height: auto; background: $background; }
    #mb-choices { color: $text-muted; padding: 0 1; }
    #minibuffer { height: 1; background: $background; }
    #mb-prompt { width: auto; color: $accent; background: $background; }
    #minibuffer Input {
        border: none;
        height: 1;
        padding: 0;
        background: $background;
        color: $foreground;
    }
    """

    BINDINGS = [
        # anywhere, even while Claude has the keys. Alt + a letter only: Textual reads
        # Alt + a digit or < > as other characters.
        # the keys come from keys.py (per platform, or [keys] in config.toml) via set_keymap
        Binding("alt+s", "show_list", "List", priority=True, id="workspace.show_list"),
        Binding(
            "alt+z", "toggle_list", "Hide/show list", priority=True, id="workspace.toggle_list"
        ),
        # in the list only (inside Claude every key is Claude's)
        Binding("enter", "resume", "Open"),
        Binding("ctrl+n", "new", "New"),
        Binding("ctrl+r", "restart", "Restart"),
        Binding("ctrl+d", "delete", "Delete"),  # asks yes/no
        Binding("x", "close", "Close"),
        Binding("a", "toggle_all", "Scope"),
        Binding("f", "filter", "Filter"),
        Binding("tab", "focus_main", "To Claude", show=False),
        Binding("l", "edit_label", "Label"),
        Binding("g", "edit_group", "Group"),
        Binding("t", "edit_tags", "Tags"),
        Binding("space", "toggle_select", "Select", show=False),
        Binding("D", "trash_sources", "Trash sources", show=False),
        Binding("n", "note", "Note"),
        Binding("q", "quit", "Quit"),
        Binding("R", "reload", "Reload"),
        Binding("ctrl+q", "quit", show=False),  # not priority: inside Claude it is Claude's
        Binding("question_mark", "toggle_keys", "Keys"),
        Binding("T", "top", "Top"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(
        self,
        cwd: str,
        notes: tuple[str, ...] = (),
        *,
        all_dirs: bool | None = None,
        only: Filter | None = None,
        config: Config | None = None,
    ) -> None:
        super().__init__()
        self.cwd = cwd
        self.config = config or Config()  # how Claude starts (launch.py), the global keys
        self.set_keymap(self.config.keys.keymap())
        self._notes = notes  # shown once at start (e.g. files moved)
        # from the command line (-a, -G, -t); None: as at the last run
        self._start_all_dirs = all_dirs
        self._start_filter = only
        self.proj = os.path.basename(cwd.rstrip("/")) or cwd
        self.rows: list[Row] = []
        self.selected: set[str] = set()  # `space`: head session ids the bulk actions act on
        self.scope: Scope = "here"  # `a` rotates it: here, subfolders, all
        self.filter = Filter()  # `f`: the list shows only these groups / tags
        self.reading = False  # a scan for a new scope is on its way
        self.plan_usage: PlanUsage | None = None  # the subscription's limits, once fetched
        self.plan_pace: plan.PlanPace | None = None  # how fast they fill
        self._cwd_key = cloud.project_key(cwd)  # what the intents of "here" are matched on
        self.intents: tuple[intents.Intent, ...] = ()  # the push queue and its latest results
        self._intents_seen = False  # the bus has been read once: show the group, even if empty
        self._modeline_text: Text | None = None  # what the mode line shows now
        self._columns_all: Scope | None = None  # the scope the header was made for
        self.live = LiveState()
        self.max_width = 0  # list width cap the rows were rendered for
        self.list_width = MIN_WIDTH  # what the content needs, within the cap
        self._pane_seq = 0
        self._last_shown: str | None = None
        self._scan_gen = 0  # only the newest scan's rows are applied
        self._polling = False  # a `claude agents` poll runs (one at a time)
        self._poll_again = False  # asked for while one ran: poll once more after it
        self._deleting = False  # a delete runs (one at a time)
        self._rescanned: set[str] = set()  # unknown sessions we already rescanned for
        self._cursor_after_scan: str | None = None
        self.real_cursor: Offset | None = None  # where the terminal's cursor is shown
        # open conversations, kept across runs (state.py)
        self.was_open: tuple[str, ...] = ()  # open at the last run, not started yet (○)
        self._last_run = OpenState(cwd=cwd)  # what the state file held at start
        self._scanned = False  # the first scan is in
        self._polled_once = False  # the first poll is in (or failed)
        self._restored = False  # the last run's state is applied
        self._saved: OpenState | None = None  # what the state file holds now
        self._save_failed = False
        self._quitting = False
        self.reload_asked = False  # quit, then main() starts the app again (R)
        # CPU / memory per process tree (usage.py), keyed by the tree's root pid
        self._round = usage.EMPTY
        self._sampling = False
        self._sampled_at = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with ContentSwitcher(initial=PLACEHOLDER, id="main"):
                yield Static(
                    "Pick a session on the right  →\nor press C-n for a new conversation",
                    id=PLACEHOLDER,
                )
            with Vertical(id="side"):
                yield DataTable(cursor_type="row", zebra_stripes=False)
                yield Static("", id="modeline")

    def on_mount(self) -> None:
        for note in self._notes:
            self.notify(note, timeout=12)
        self._last_run = state.load(self.cwd)
        last = self._last_run
        self.scope = "all" if self._start_all_dirs else "here"  # never the last run's
        self.filter = last.filter if self._start_filter is None else self._start_filter
        self.reading = True
        self._render()  # empty table and mode-line until the scan returns
        self.reload()
        self.query_one(DataTable).focus()
        self.refresh_live(SHARE_SECONDS)
        self.set_interval(POLL_SECONDS, lambda: self.refresh_live(SHARE_SECONDS))
        if usage.available():
            self.set_interval(SAMPLE_SECONDS, self._sample_tick)
        self._plan_tick()
        self._intents_tick()
        self.set_interval(INTENT_POLL_SECONDS, self._intents_tick)

    def _intents_tick(self) -> None:
        self.run_worker(self._intents_fetch, thread=True, group="intents", exclusive=True)

    def _intents_fetch(self) -> None:
        self._to_ui(self._intents_got, intents.load(time.time()))

    def _intents_got(self, found: tuple[intents.Intent, ...]) -> None:
        if found != self.intents or not self._intents_seen:
            self.intents, self._intents_seen = found, True
            self._modeline()

    def _plan_tick(self) -> None:
        self.run_worker(self._plan_fetch, thread=True, group="plan", exclusive=True)
        wait = PLAN_POLL_SECONDS - time.time() % PLAN_POLL_SECONDS
        self.set_timer(wait + 0.05, self._plan_tick)  # on the next beat of the wall clock

    def _plan_fetch(self) -> None:
        now = time.time()
        found = plan.shared(now - now % PLAN_SECONDS)  # this minute's sample, or fetch it
        self._to_ui(self._plan_got, found)

    def _plan_got(self, found: plan.Shared | None) -> None:
        if found and (found.usage, found.pace) != (self.plan_usage, self.plan_pace):
            # a failed ask has no sample: the last one stays
            self.plan_usage, self.plan_pace = found.usage, found.pace
            self._modeline()

    def on_resize(self, _event) -> None:
        if self._max_width() != self.max_width:
            self._render()  # the window changed: re-cut the text

    # --- panes -----------------------------------------------------------

    def _panes(self) -> list[ClaudePane]:
        return list(self.query(ClaudePane))

    def _shown_pane(self) -> ClaudePane | None:
        current = self.query_one("#main", ContentSwitcher).current
        if not current or current == PLACEHOLDER:
            return None
        try:
            return self.query_one(f"#{current}", ClaudePane)
        except NoMatches:
            return None

    def shown_session(self) -> str | None:
        """The session the main pane shows."""
        pane = self._shown_pane()
        return pane.session_id if pane else None

    def _pane_for(self, session_ids: tuple[str, ...]) -> ClaudePane | None:
        return next(
            (p for p in self._panes() if p.session_id in session_ids and not p.closing), None
        )

    def _retag(self, pane: ClaudePane) -> None:
        """← inside Claude: the title names the session the pane shows now.

        Each new title is matched once, and only against a `claude agents` snapshot
        taken after it arrived: an older one can still carry old names (a renamed
        session would match another agent of that name). A title that names no listed
        agent yet (a continuation ← just made) is tried again with the next snapshots.
        """
        if pane.title_matched or self.live.taken_at < pane.title_at:
            return
        seen = agents.session_for_title(pane.title_seen, self.live.agents, pane.session_id)
        if seen is None:
            return
        pane.title_matched = True
        if seen != pane.session_id:
            pane.session_id = seen
            if not any(seen in r.ids for r in self.rows) and seen not in self._rescanned:
                self._rescanned.add(seen)  # e.g. ← made a continuation: its transcript is new
                self.reload()

    def on_claude_pane_title_seen(self, _message: ClaudePane.TitleSeen) -> None:
        self.refresh_live()  # a snapshot newer than the title, to match it with

    def on_claude_pane_cursor_moved(self, message: ClaudePane.CursorMoved) -> None:
        if message.pane is self._shown_pane():
            self._place_real_cursor(message.cell)

    def _place_real_cursor(self, cell: Offset | None) -> None:
        """Show the terminal's real cursor at this cell, or hide it (None).

        Textual keeps the real cursor hidden and only moves it (for IME); there is no
        public call to show it, so this writes the two escape codes to the driver.
        """
        if cell == self.real_cursor:
            return
        self.real_cursor = cell
        driver = self._driver
        if cell is None:
            if driver is not None:
                driver.write("\x1b[?25l")
            return
        self.cursor_position = cell  # where Textual leaves it after each frame
        if driver is not None:
            driver.write(f"\x1b[{cell.y + 1};{cell.x + 1}H\x1b[?25h")

    def on_claude_pane_exited(self, message: ClaudePane.Exited) -> None:
        if message.pane is self._shown_pane():
            self.action_show_list()  # don't leave the keys in a terminal that ended
        self._refresh_marks()
        self._save_state()  # ended by you (/exit): not reopened next time

    # --- width -----------------------------------------------------------

    def _max_width(self) -> int:
        return max(MIN_WIDTH, self.size.width // 2)

    def _pin(self) -> None:
        self.query_one("#side").styles.width = self.list_width

    # --- rendering -------------------------------------------------------

    @staticmethod
    def _session_text(r: Row, width: int, selected: bool = False) -> Text:
        """Title (+ tags), then where it works (cwd · branch), then its rooms if any, dimmed."""
        # Tags are chips (reverse-video cyan) so they do not read as part of the title.
        text = Text("✓ " if selected else "", style="bold green")
        text.append((f"[{r.meta.group}] " if r.meta.group else "") + r.display_title)
        for t in r.meta.tags:
            text.append("  ")
            text.append(f" {t} ", style=TAG_STYLE)
        if r.notes:
            text.append(f"  ✎ {notes.label(r.notes)}")
        text.truncate(width)
        # the branch gets a third of the width at most; the cwd takes what is left
        branch = ""
        if r.git_branch and r.git_branch != "HEAD":
            branch = r.git_branch[: width // 3]
        tail = f" · {branch}" if branch else ""
        room_left = width - len(tail)
        where = (short_path(r.cwd, room_left) if room_left >= 2 else "") + tail
        where = where if room_left >= 2 else where.lstrip(" ·")
        text.append("\n" + where, style="dim")
        room_text = rooms.label(r.rooms)
        if room_text:
            if len(room_text) > width:
                room_text = room_text[: max(width - 1, 0)] + "…"
            text.append("\n" + room_text, style="dim")
        return text

    def _agent(self, r: Row) -> AgentState | None:
        """Live state of the conversation: its newest session that Claude reports."""
        return next((st for sid in reversed(r.ids) if (st := self.live.agents.get(sid))), None)

    def _mark(self, r: Row, shown: str | None, open_ids: set[str]) -> str:
        if shown in r.ids:
            return "▶"
        st = self._agent(r)
        if st and st.needs_input:
            return "⏳"
        if st and st.working:
            return "⚙"
        if (st and st.alive) or open_ids.intersection(r.ids):
            return "●"
        if any(sid in r.ids for sid in self.was_open):
            return "○"
        return ""

    def _open_ids(self) -> set[str]:
        return {p.session_id for p in self._panes() if not p.closing}  # removal is not instant

    def _modeline(self) -> None:
        """Groups of lines, a rule between each. Line 1: what the main pane shows. Line 2:
        its cabildo agent name, when a session is shown. Next: its cwd (or the key hints). Then the shown conversation's model and context, its
        last reply's effort (left out when not known), and tokens. Then, on a subscription, its
        usage limits. Last, a CPU and memory
        table, right-aligned: the shown conversation, all running ones (left out when the
        same), this app. The context and plan lines turn amber, then red, as the window fills.
        """
        n = len(self.rows)
        pane = self._shown_pane()
        shown = pane.session_id if pane else None
        row = next((r for r in self.rows if shown in r.ids), None) if shown else None
        width = self.list_width - BORDER - 2  # mode-line padding
        if row:
            top, bottom = f"▶ {row.display_title}", short_path(row.cwd, width)
        elif pane:  # not in this list: a new conversation, or another directory's
            st = self.live.agents.get(shown) if shown else None
            name = (st.name if st else "") or (
                (pane.title_seen or shown[:8]) if shown else "new conversation"
            )
            top = f"▶ {name}"
            bottom = short_path(st.cwd if st else pane.launch_cwd or self.cwd, width)
        else:
            scope = SCOPE_NAMES[self.scope].format(proj=self.proj)
            if self.filter.active:
                scope = f"{scope} {self.filter.text()}"
            top = f"{scope}  ·  {n} session{'' if n == 1 else 's'}"
            bottom = "RET open · C-n new · ? keys" if n else "C-n new · a scope · f filter · ? keys"
        context = fmt_context(row.tokens) if row else ""
        tokens = fmt_tokens(row.tokens, width) if row else ""
        use = self._row_use(row) if row else None
        if pane and pane.exited:
            top = f"{top} (ended)"
        if self.reading:
            bottom = "reading transcripts…"
        effort = fmt_effort(row.tokens) if row else ""
        ctx_style = context_style(row.tokens) if row else ""
        # the cabildo name other agents address this one by (deterministic from the id)
        agent = [(f"@ {names.robot_name(shown)}", "bold")] if shown else []
        groups: list[list[tuple[str, str]]] = [[(top, ""), *agent, (bottom, "")]]
        if context or tokens:
            groups.append([(context, ctx_style), *([(effort, "")] if effort else []), (tokens, "")])
        if self.plan_usage:
            style = plan_style(self.plan_usage)
            groups.append([(x, style) for x in fmt_plan(self.plan_usage, self.plan_pace)])
        if self.intents or (self._intents_seen and store.db_path().exists()):
            groups.append(
                fmt_intents(
                    intents.in_scope(self.intents, self.scope, self.cwd, self._cwd_key), time.time()
                )
            )
        if usage.available():
            this = fmt_use("this one", use, self._row_first_mb(row) if row else None)
            everyone, me = self._usage_lines()
            running = set(self.uses) - {os.getpid()}
            same = bool(row and running and running <= self._row_pids(row))  # only this one
            table = [USE_HEADER, this, *([] if same else [everyone]), me]
            groups.append([(x, "") for x in table])
        rule = [("─" * width, "dim")]
        lines = [line for i, g in enumerate(groups) for line in (rule if i else []) + g]
        text = Text("\n").join(Text(x[:width], style=style) for x, style in lines)
        if text == self._modeline_text:
            return  # an update repaints the whole screen, the list too (#9)
        same_height = self._modeline_text is not None and (
            len(text.plain.split("\n")) == len(self._modeline_text.plain.split("\n"))
        )
        self._modeline_text = text
        self.query_one("#modeline", Static).update(text, layout=not same_height)

    def reload(self) -> None:
        """Rescan transcripts in a worker thread, then rebuild the table.

        The first read of every directory takes seconds (transcripts reach hundreds
        of MB); after that only what Claude appended is read.
        """
        self._scan_gen += 1
        gen = self._scan_gen
        cwd = None if self.all_dirs else self.cwd
        only, sub = self.filter, self.scope == "subfolders"
        self.run_worker(lambda: self._scan(gen, cwd, only, sub), thread=True, group="scan")

    def _scan(self, gen: int, cwd: str | None, only: Filter, subfolders: bool) -> None:
        worker = get_current_worker()
        try:
            rows = sessions.build_rows(cwd, lambda: worker.is_cancelled, only, subfolders)
            rows = sessions.attach_notes(sessions.attach_rooms(rows))
        except sessions.Cancelled:
            return  # the app is closing
        self._to_ui(self._apply_rows, gen, rows)

    def _to_ui(self, callback: Callable[..., object], *args: object, **kwargs: object) -> None:
        """From a worker thread: run the callback on the UI thread, unless the app is
        closing by then. On exit Textual cancels the workers but cannot stop their
        threads, and the widgets are being removed.
        """
        worker = get_current_worker()

        def unless_closing() -> None:
            if not worker.is_cancelled:
                callback(*args, **kwargs)

        self.call_from_thread(unless_closing)

    def _apply_rows(self, gen: int, rows: list[Row]) -> None:
        if gen != self._scan_gen:
            return  # a newer scan is on its way
        self.reading = False
        # where the cursor goes: a row chosen before the scan (delete), else where it was
        saved = self._cursor_after_scan or self._cursor_session_id()
        moved = self._cursor_after_scan is not None
        self._cursor_after_scan = None
        same_order = [r.head.session_id for r in rows] == [r.head.session_id for r in self.rows]
        self.rows = rows
        self.selected &= {r.head.session_id for r in rows}
        if not (
            same_order and not moved and self._columns_all == self.scope and self._update_in_place()
        ):
            self._render(saved)
        self._scanned = True
        self.call_later(self._restore)

    def _layout(self) -> tuple[list[Text], int]:
        """The rows' text cut to the width cap, and the list width they need."""
        age_width = max([3] + [len(r.age) for r in self.rows])  # "Age" header
        text_width = max(1, self._max_width() - CHROME - age_width)
        texts = [
            self._session_text(r, text_width, r.head.session_id in self.selected) for r in self.rows
        ]
        content = max([cell_len(line) for t in texts for line in t.plain.split("\n")], default=0)
        return texts, min(self._max_width(), max(MIN_WIDTH, content + age_width + CHROME))

    def _update_in_place(self) -> bool:
        """Same rows in the same order: refresh ages and text without rebuilding the table
        (no flicker, the scroll stays). False when the width would change.
        """
        texts, width = self._layout()
        table = self.query_one(DataTable)
        if (
            width != self.list_width
            or self._max_width() != self.max_width
            or table.row_count != len(self.rows)
        ):
            return False
        for i, (r, text) in enumerate(zip(self.rows, texts, strict=True)):
            if table.get_cell_at(Coordinate(i, 1)) != r.age:
                table.update_cell_at(Coordinate(i, 1), r.age)
            old = str(table.get_cell_at(Coordinate(i, 2)))
            if old.count("\n") != text.plain.count("\n"):
                return False  # the row height changes: only a rebuild sets it
            if old != text.plain:
                table.update_cell_at(Coordinate(i, 2), text)
        self._refresh_marks()
        self._refresh_use()
        return True

    def _render(self, saved: str | None = None) -> None:
        """Fill the table, cutting text to the width cap, then size the list to fit."""
        if saved is None:
            saved = self._cursor_session_id()
        self.max_width = self._max_width()
        texts, self.list_width = self._layout()
        shown, open_ids = self.shown_session(), self._open_ids()
        table = self.query_one(DataTable)
        if self._columns_all != self.scope:
            table.clear(columns=True)
            table.add_column(" ", width=MARK_WIDTH)
            table.add_columns("Age", SCOPE_HEADERS[self.scope])
            table.add_column("Use", width=USE_WIDTH)
            self._columns_all = self.scope
        else:
            table.clear()
        for r, text in zip(self.rows, texts, strict=True):
            table.add_row(
                self._mark(r, shown, open_ids),
                r.age,
                text,
                self._use_text(r),
                key=r.head.session_id,
                height=text.plain.count("\n") + 1,
            )
        # its row is gone (e.g. `a` back to this directory): the shown conversation's row
        if not (saved and self._restore_cursor(saved)) and shown:
            self._restore_cursor(shown)
        self._follow_shown()
        self._modeline()
        self._pin()

    def _refresh_marks(self) -> None:
        """Update the marks; when the shown session changed (e.g. ← in Claude), follow it."""
        shown, open_ids = self.shown_session(), self._open_ids()
        table = self.query_one(DataTable)
        if table.row_count == len(self.rows):
            for i, r in enumerate(self.rows):
                mark = self._mark(r, shown, open_ids)
                if table.get_cell_at(Coordinate(i, 0)) != mark:  # redraw only what changed
                    table.update_cell_at(Coordinate(i, 0), mark)
        self._follow_shown()
        self._modeline()

    def _follow_shown(self) -> None:
        """When the main pane shows another session, the cursor goes to its row; a session
        with no row yet (a new conversation before its first message) is followed when
        its row appears.
        """
        shown = self.shown_session()
        if shown != self._last_shown and (not shown or self._restore_cursor(shown)):
            self._last_shown = shown

    # --- live markers, polled off the UI thread --------------------------

    def refresh_live(self, max_age: float = 0.0) -> None:
        """Poll `claude agents` in a worker thread, one poll at a time (each costs ~0.4 s
        CPU and ~170 MB). Asked while one runs, it polls once more after it: the running
        poll may have started before what asked for it. So snapshots arrive in order.
        ``max_age``: a snapshot another workspace took up to this many seconds ago is
        good enough (timed polls); 0 asks for one taken from now on (events).
        """
        if self._polling:
            self._poll_again = True
            return
        self._polling = True
        self.run_worker(lambda: self._poll(max_age), thread=True, group="poll")

    def _poll(self, max_age: float) -> None:
        started = time.monotonic()
        found = None
        try:
            snap = agents.shared_live_agents(not_before=time.time() - max_age)
            found = snap.agents
            started = time.monotonic() - (time.time() - snap.taken_at)  # it may be shared
        finally:
            self._to_ui(self._polled, found, started)

    def _polled(self, found: dict[str, AgentState] | None, started: float) -> None:
        self._polling = False
        self._polled_once = True
        if found is not None:  # None: `claude agents` failed; keep the last snapshot
            self._apply_live(LiveState(agents=found, loaded=True, taken_at=started))
        self.reload()  # ages and order stay current (cached scan)
        if self._poll_again:
            self._poll_again = False
            self.refresh_live()
        self.call_later(self._restore)

    def _apply_live(self, state: LiveState) -> None:
        self.live = state
        by_pid = {
            st.pid: st.session_id
            for st in state.agents.values()
            if st.pid and st.kind == "interactive"
        }
        for pane in self._panes():
            if not pane.session_id and pane.pid is not None and (sid := by_pid.get(pane.pid)):
                pane.session_id = sid  # a new conversation: Claude lists its process and id
                if pane.new_group:
                    self._join_group(sid, pane.new_group)
            self._retag(pane)  # a continuation made by ← may only now be listed
        self._refresh_marks()
        self._save_state()  # a new conversation's id, a ← switch

    # --- CPU, wakeups and memory of the sessions (usage.py) ------------------

    def _usage_roots(self, everything: bool) -> dict[int, bool]:
        """Pid -> with its children: every Claude this app runs (its pane), the Claudes
        `claude agents` lists with a process (those of the list's conversations, or all
        of them for the top view), and this app itself (alone: its children are the
        panes).
        """
        roots = {p.pid: True for p in self._panes() if p.pid and not p.closing}
        listed = {sid for r in self.rows for sid in r.ids}
        roots |= {
            st.pid: True
            for st in self.live.agents.values()
            if st.pid and (everything or st.session_id in listed)
        }
        roots[os.getpid()] = False
        return roots

    def _sample_tick(self, force: bool = False) -> None:
        """Sample only while someone can see it: every SAMPLE_SECONDS while the list
        shows (an interval timer), every TOP_SAMPLE_SECONDS after the last sample while
        the top view is open (`_sampled` sets the next timer), never while hidden.
        """
        top = isinstance(self.screen, TopScreen)
        if self._sampling:
            return
        if not force and time.monotonic() - self._sampled_at < SAMPLE_SECONDS - 0.2:
            return
        if not top and not self.query_one("#side").display:
            return
        self._sampling = True
        roots, last = self._usage_roots(everything=top), self._round
        self.run_worker(lambda: self._sample(roots, last, top), thread=True, group="usage")

    def _sample(self, roots: dict[int, bool], last: usage.Round, wakeups: bool) -> None:
        started = time.monotonic()
        done = last
        try:
            done = usage.sample_all(roots, last, wakeups=wakeups)
        finally:
            self._to_ui(self._sampled, done, started)

    @property
    def uses(self) -> dict[int, Use]:
        """Root pid -> its tree's use over the last sampling interval."""
        return self._round.uses

    def _sampled(self, done: usage.Round, at: float) -> None:
        self._round = done
        self._sampling = False
        self._sampled_at = at
        self._refresh_use()
        if isinstance(self.screen, TopScreen):
            self.screen.show(self._top_lines())
            wait = TOP_SAMPLE_SECONDS if done.uses else TOP_FIRST_SECONDS
            self.set_timer(wait, lambda: self._sample_tick(force=True))

    def _row_pids(self, r: Row) -> set[int]:
        """The conversation's processes: its panes here and its agents elsewhere."""
        pids = {p.pid for p in self._panes() if p.session_id in r.ids and p.pid}
        return pids | {st.pid for sid in r.ids if (st := self.live.agents.get(sid)) and st.pid}

    def _row_use(self, r: Row) -> Use | None:
        """The conversation's use over the last sampling interval."""
        found = [self.uses[pid] for pid in self._row_pids(r) if pid in self.uses]
        return Use.total(found) if found else None

    def _row_first_mb(self, r: Row) -> float | None:
        """Memory of a conversation sampled once so far (it just started); None if never."""
        found = [s for pid in self._row_pids(r) if (s := self._round.samples.get(pid))]
        return sum(p.anon_kb for s in found for p in s.procs) / 1024 if found else None

    def _use_text(self, r: Row) -> str:
        u = self._row_use(r)
        return f"{fmt_cpu(u.cpu):>{USE_WIDTH}}\n{fmt_mb(u.mem_mb):>{USE_WIDTH}}" if u else ""

    def _refresh_use(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == len(self.rows):
            for i, r in enumerate(self.rows):
                text = self._use_text(r)
                if table.get_cell_at(Coordinate(i, 3)) != text:  # redraw only what changed
                    table.update_cell_at(Coordinate(i, 3), text)
        self._modeline()

    def _usage_lines(self) -> tuple[str, str]:
        """Mode-line rows 7 and 8: the list's running conversations together, and this app.
        The mode line leaves out the first when it equals the "this one" row.
        """
        others = [u for pid, u in self.uses.items() if pid != os.getpid()]
        total = Use.total(others) if others else None
        return fmt_use("all running", total), fmt_use("workspace", self.uses.get(os.getpid()))

    def _root_label(self, pid: int) -> str:
        """Which session a sampled tree is, and where it runs."""
        pane = next((p for p in self._panes() if p.pid == pid), None)
        st = next((a for a in self.live.agents.values() if a.pid == pid), None)
        sid = pane.session_id if pane else st.session_id if st else ""
        row = next((r for r in self.rows if sid in r.ids), None) if sid else None
        name = (
            (row.display_title if row else "")
            or (st.name if st else "")
            or (pane.title_seen if pane else "")
            or (sid[:8] if sid else "new conversation")
        )
        where = "" if pane else " (background)" if st and st.attachable else " (other terminal)"
        shown = "▶ " if pane is not None and pane is self._shown_pane() else ""
        return f"{shown}{name}{where}"

    def _top_lines(self) -> list[tuple[Text, str, str, str]]:
        me = os.getpid()
        trees = sorted(
            ((pid, u) for pid, u in self.uses.items() if pid != me),
            key=lambda item: item[1].cpu,
            reverse=True,
        )
        lines: list[tuple[Text, str, str, str]] = []

        def line(label: Text, u: Use | usage.ProcUse) -> tuple[Text, str, str, str]:
            wake = "" if u.wakeups is None else f"{u.wakeups:.0f}"
            return label, fmt_cpu(u.cpu), wake, fmt_mb(u.mem_mb)

        for pid, u in trees:
            lines.append(line(Text(self._root_label(pid)[:48], style="bold"), u))
            for proc in u.procs[:TOP_PROCS]:
                lines.append(line(Text(f"   {proc.comm} {proc.pid}"[:48], style="dim"), proc))
            if len(u.procs) > TOP_PROCS:
                lines.append(
                    (Text(f"   … {len(u.procs) - TOP_PROCS} more", style="dim"), "", "", "")
                )
            if u.ended_cpu >= 0.5:
                lines.append(
                    (Text("   (ended in between)", style="dim"), fmt_cpu(u.ended_cpu), "", "")
                )
        if me in self.uses:
            lines.append(line(Text("workspace (this app)", style="bold"), self.uses[me]))
        if self.uses:
            lines.append(line(Text("Total", style="bold"), Use.total(list(self.uses.values()))))
        else:
            lines.append((Text("measuring…"), "", "", ""))
        return lines

    def action_top(self) -> None:
        if not usage.available():
            self.notify("CPU and memory per session need Linux /proc", severity="warning")
            return
        self.push_screen(TopScreen(self._top_lines()))
        self.call_later(lambda: self._sample_tick(force=True))  # now, not in up to 10 s

    # --- helpers ---------------------------------------------------------

    def _current_row(self) -> Row | None:
        if not self.rows:
            return None
        return self.rows[self.query_one(DataTable).cursor_row]

    def _cursor_session_id(self) -> str | None:
        r = self._current_row()
        return r.head.session_id if r else None

    def _restore_cursor(self, session_id: str) -> bool:
        for i, r in enumerate(self.rows):
            if session_id in r.ids:
                self.query_one(DataTable).move_cursor(row=i)
                return True
        return False

    def _target(self, r: Row) -> Target | None:
        """How to open a conversation: attach to its background session, else resume it;
        None when it cannot be resumed because its directory is gone.

        A continuation (made by ← in Claude) is a background session whose own
        transcript holds no messages, so `--resume` cannot load it. This directory's
        conversations start where the app runs; another directory's in its own.
        """
        s = r.resumable
        here = s.path.parent in sessions.project_dirs(self.cwd)
        cwd = None if here else sessions.launch_dir(s)
        st = self.live.agents.get(r.head.session_id)
        attach = (st is not None and st.attachable) or (not self.live.loaded and len(r.chain) > 1)
        if attach:  # the agent runs on its own: any directory will do
            agent_id = st.agent_id if st else r.head.session_id[:8]
            return Target(
                session_ids=r.ids,
                session_id=r.head.session_id,
                command=(CLAUDE, "attach", agent_id),
                cwd=cwd,
            )
        if not here and cwd is None:
            return None
        return Target(
            session_ids=r.ids,
            session_id=s.session_id,
            command=self.config.launch_for(r.meta.group).command(CLAUDE, "--resume", s.session_id),
            cwd=cwd,
        )

    def _join_group(self, session_id: str, group: str) -> None:
        """Put a new conversation in ``group``, unless it got one meanwhile (``g``)."""
        self._update_meta(session_id, lambda m: m if m.group else replace(m, group=group))

    def _update_meta(self, key: str, change: Callable[[Meta], Meta]) -> None:
        sessions.update_meta(key, change)
        self.reload()

    # --- actions ---------------------------------------------------------

    async def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        await self.action_resume()  # DataTable consumes Enter and emits this

    async def action_resume(self) -> None:
        """Show the conversation in the main pane (start its Claude the first time) and focus it."""
        r = self._current_row()
        if r:
            await self._open(r)

    async def _open(self, r: Row, focus: bool = True) -> None:
        pane = self._pane_for(r.ids)
        if pane is not None and pane.exited:  # Claude had ended: start it again
            await pane.remove()
            pane = None
        if pane is None:
            target = self._target(r)
            if target is None:
                self.notify(
                    f"Not opened: its directory {short_path(r.cwd, 50)} is gone", severity="warning"
                )
                return
            pane = await self._mount(target)
        self.was_open = tuple(sid for sid in self.was_open if sid not in r.ids)
        self._show(pane, focus)

    async def action_new(self) -> None:
        """Start a new conversation in the list's directory and give it the keys.

        Its session id comes from `claude agents` (the row of the pane's process); its
        row appears once its transcript exists, after the first message.
        """
        for pane in self._panes():
            if pane.exited and not pane.session_id:  # ended before it had an id: nothing to keep
                await pane.remove()
        # a filter on one group: the new conversation joins it, so it stays in the list
        group = self.filter.groups[0] if len(self.filter.groups) == 1 else ""
        command = self.config.launch_for(group).command(CLAUDE)
        target = Target(session_ids=(), session_id="", command=command, group=group)
        self._show(await self._mount(target))
        self.set_timer(NEW_POLL_SECONDS, self.refresh_live)

    async def _mount(self, target: Target) -> ClaudePane:
        self._pane_seq += 1
        pane = ClaudePane(target, f"pane-{self._pane_seq}")
        await self.query_one("#main", ContentSwitcher).mount(pane)
        return pane

    def _show(self, pane: ClaudePane, focus: bool = True) -> None:
        self.query_one("#main", ContentSwitcher).current = pane.id
        if focus:
            pane.focus()
        self._refresh_marks()
        self.refresh_live()  # a new Claude: its state shows without waiting a poll
        self._save_state()

    # --- open conversations across runs (state.py) -------------------------

    async def _restore(self) -> None:
        """Once the first scan and the first poll are in: mark what was open at the last
        run (○) and start the one that was shown; the others start on Enter. Not when
        that conversation runs in another terminal (two Claudes on one transcript).
        """
        if self._restored or not (self._scanned and self._polled_once):
            return
        self._restored = True
        last, have = self._last_run, self._open_ids()
        self.was_open = tuple(sid for sid in last.open if sid not in have)
        row = next((r for r in self.rows if last.shown in r.ids), None) if last.shown else None
        st = self._agent(row) if row else None
        elsewhere = st is not None and st.kind == "interactive" and st.pid is not None
        if row and self._shown_pane() is None and not elsewhere and last.shown in self.was_open:
            await self._open(row, focus=False)  # the keys stay in the list
        self._refresh_marks()
        self._save_state()

    def _open_state(self) -> OpenState:
        ids: list[str] = []
        for p in self._panes():  # not: ended by you (/exit), or closed by x / delete
            if p.session_id and not p.exited and not p.closing and p.session_id not in ids:
                ids.append(p.session_id)
        ids += [sid for sid in self.was_open if sid not in ids]
        shown = self.shown_session()
        return OpenState(
            cwd=self.cwd,
            all_dirs=self.all_dirs,
            filter=self.filter,
            open=tuple(ids),
            shown=shown if shown in ids else None,
        )

    def _save_state(self) -> None:
        """Write what is open at every change, so it is current even after a crash.
        Not before the restore: until then the file holds what to restore.
        """
        if self._quitting or not self._restored:
            return
        now = self._open_state()
        if now == self._saved:
            return
        try:
            state.save(now)
        except OSError as exc:
            if not self._save_failed:
                self._save_failed = True
                self.notify(f"Open conversations not saved: {exc}", severity="warning")
            return
        self._saved = now

    def action_focus_main(self) -> None:
        pane = self._shown_pane()
        if pane:
            pane.focus()

    def action_show_list(self) -> None:
        self.query_one("#side").display = True
        self.query_one(DataTable).focus()

    def action_hide_list(self) -> None:
        self.query_one("#side").display = False
        self.action_focus_main()

    def action_toggle_list(self) -> None:
        if self.query_one("#side").display:
            self.action_hide_list()
        else:
            self.query_one("#side").display = True

    @property
    def all_dirs(self) -> bool:
        """The list shows every directory's conversations."""
        return self.scope == "all"

    def action_toggle_all(self) -> None:
        """`a`: this directory's conversations, then with its subfolders', then every
        directory's, then back.
        """
        self.scope = SCOPES[(SCOPES.index(self.scope) + 1) % len(SCOPES)]
        self.reading = True
        self._modeline()
        self.reload()
        self._save_state()

    def action_filter(self) -> None:
        """`f`: show only some groups / tags, as `g:work t:urgent`; empty shows all."""
        meta = sessions.load_meta().values()
        groups = sorted({m.group for m in meta if m.group}, key=str.lower)
        tags = sorted({t for m in meta for t in m.tags}, key=str.lower)
        choices = tuple(Filter(groups=(g,)).text() for g in groups) + tuple(
            Filter(tags=(t,)).text() for t in tags
        )

        def done(value: str | None) -> None:
            if value is None:
                return
            try:
                only = Filter.parse(value)
            except ValueError as exc:
                self.notify(f"Filter not changed: {exc}", severity="warning")
                return
            if only != self.filter:
                self.filter = only
                self.reading = True
                self._modeline()
                self.reload()
                self._save_state()

        self.push_screen(EditScreen("Filter:", self.filter.text(), choices, words=True), done)

    def action_toggle_keys(self) -> None:
        self.push_screen(KeysScreen(keys_help(self.config.keys)))

    def action_quit(self) -> None:  # type: ignore[override]
        """Close every Claude, then the app; asks first while any Claude runs."""
        self._ask_quit("close and quit?")

    def action_reload(self) -> None:
        """`R`: quit and start again, on the code installed now. What was open is marked
        (○) and the shown conversation starts again, as at any start.
        """
        self._ask_quit("close and reload?", reload=True)

    def _ask_quit(self, question: str, reload: bool = False) -> None:
        def go() -> None:
            self.reload_asked = reload
            self._quit()

        running = [p for p in self._panes() if not p.exited]
        if not running:
            go()
            return

        def done(answer: str | None) -> None:
            if answer is not None and answer.strip().lower() == "yes":
                go()

        n = len(running)
        self.push_screen(
            EditScreen(
                f"{n} Claude session{'s' if n > 1 else ''} running; {question} (yes or no)", ""
            ),
            done,
        )

    def _quit(self) -> None:
        self._save_state()  # with every pane, before they close
        self._quitting = True  # their exits below must not drop them from it
        for pane in self._panes():
            pane.close()
        self.exit()

    # --- close -----------------------------------------------------------

    def action_close(self) -> None:
        """`x`: close the conversation in the app. Its Claude ends (a background session
        keeps running on its own), the transcript stays, and the next start does not
        open it again. Asks while its Claude runs.
        """
        r = self._current_row()
        if not r:
            return
        panes = [p for p in self._panes() if p.session_id in r.ids]
        if not panes and not any(sid in r.ids for sid in self.was_open):
            self.notify("Not open")
            return

        def close() -> None:
            self._close_panes(r.ids)
            self.was_open = tuple(sid for sid in self.was_open if sid not in r.ids)
            self._refresh_marks()
            self._save_state()

        if not any(not p.exited for p in panes):
            close()
            return

        def done(answer: str | None) -> None:
            if answer is not None and answer.strip().lower() == "yes":
                close()

        self.push_screen(
            EditScreen(
                f'Close "{r.display_title[:60]}"? Its Claude ends; the conversation stays '
                "(yes or no)",
                "",
            ),
            done,
        )

    # --- restart ---------------------------------------------------------

    async def action_restart(self) -> None:
        """`C-r`: end the conversation's Claude and start it again on the same transcript
        (e.g. after a settings or MCP change). Not open yet: it starts, like Enter.
        """
        r = self._current_row()
        if not r:
            return
        pids = self._close_panes(r.ids)
        if not pids:
            await self._open(r)
            return
        self.run_worker(lambda: self._restart(r, pids), thread=True, group="restart")

    def _restart(self, r: Row, pids: list[int]) -> None:
        """Worker thread: wait until the old Claude is gone (two never write one
        transcript), then open the conversation again.
        """
        agents.wait_exit(pids)
        self._to_ui(self.call_later, self._open, r)

    # --- delete ----------------------------------------------------------

    def action_delete(self) -> None:
        r = self._current_row()
        if not r:
            return
        if self._deleting:
            self.notify("A delete is still running", severity="warning")
            return

        def done(answer: str | None) -> None:
            if answer is not None and answer.strip().lower() == "yes" and not self._deleting:
                self._deleting = True
                self.run_worker(lambda: self._delete(r), thread=True, group="delete")

        self.push_screen(EditScreen(f'Delete "{r.display_title[:60]}"? (yes or no)', ""), done)

    def _delete(self, r: Row) -> None:
        """Worker thread: stop everything that runs the conversation, then trash it."""
        try:
            self._delete_conversation(r)
        finally:
            self._to_ui(self._delete_finished)

    def _delete_finished(self) -> None:
        self._deleting = False

    def _delete_conversation(self, r: Row) -> None:
        live = agents.live_agents()
        if live is None:  # unknown is not "runs nowhere": a Claude may still write
            self._to_ui(
                self.notify,
                "Not deleted: `claude agents` failed, so where the conversation runs is not known",
                severity="error",
            )
            return
        ours = self.call_from_thread(self._pane_pids)
        if any(
            (st := live.get(sid)) and st.kind == "interactive" and st.pid and st.pid not in ours
            for sid in r.ids
        ):
            self._to_ui(
                self.notify, "Running in another terminal: exit it there first", severity="warning"
            )
            return
        pids: list[int] = []
        for sid in r.ids:
            st = live.get(sid)
            if st and st.attachable:
                if st.pid:
                    pids.append(st.pid)
                if err := agents.remove(st.agent_id):
                    self._to_ui(self.notify, f"Not deleted: {err}", severity="error")
                    return
        pids += self.call_from_thread(self._close_panes, r.ids)
        agents.wait_exit(pids)
        dest = sessions.trash(r)
        self._to_ui(self._deleted, r, str(dest))

    def _pane_pids(self) -> set[int]:
        return {p.pid for p in self._panes() if p.pid}

    def _close_panes(self, session_ids: tuple[str, ...]) -> list[int]:
        """End and remove every pane showing one of these sessions; return their pids."""
        switcher = self.query_one("#main", ContentSwitcher)
        pids: list[int] = []
        for pane in self._panes():
            if pane.session_id in session_ids:
                if pane.pid:
                    pids.append(pane.pid)
                if switcher.current == pane.id:
                    switcher.current = PLACEHOLDER
                pane.close()
                pane.remove()
        return pids

    def _deleted(self, r: Row, dest: str) -> None:
        # the cursor goes to the row above the deleted one (below, if it was the first)
        ids = [row.head.session_id for row in self.rows]
        i = ids.index(r.head.session_id) if r.head.session_id in ids else None
        if i is not None and len(ids) > 1:
            self._cursor_after_scan = ids[i - 1] if i > 0 else ids[1]
        self.was_open = tuple(sid for sid in self.was_open if sid not in r.ids)
        self._save_state()
        self.reload()
        self.refresh_live()
        self.notify(f'Deleted "{r.display_title[:30]}" (trash: {short_path(dest, 40)})')

    # --- list movement ---------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    # --- metadata edits ----------------------------------------------------

    def _edit(
        self,
        prompt: str,
        current: Callable[[Row], str],
        apply: Callable[[str, str], None],
        choices: tuple[str, ...] = (),
    ) -> None:
        r = self._current_row()
        if not r:
            return
        key = r.meta_key

        def done(value: str | None) -> None:
            if value is not None:
                apply(key, value)

        self.push_screen(EditScreen(prompt, current(r), choices), done)

    def action_edit_group(self) -> None:
        # every group in use, in any directory (the sidecar is global, not this list)
        groups = sorted({m.group for m in sessions.load_meta().values() if m.group}, key=str.lower)
        self._edit(
            "Group:",
            lambda r: r.meta.group,
            lambda key, v: self._update_meta(key, lambda m: replace(m, group=v.strip())),
            tuple(groups),
        )

    def action_edit_label(self) -> None:
        self._edit(
            "Label (blank = Claude's title):",
            lambda r: r.meta.label,
            lambda key, v: self._update_meta(key, lambda m: replace(m, label=v.strip())),
        )

    def action_note(self) -> None:
        r = self._current_row()
        if not r:
            return

        def done(value: str | None) -> None:
            if value is None:
                return
            if notes.save(r.notes, r.head.session_id, value):
                self.reload()
            else:
                self.notify("Notes not saved: the bus is not writable")

        self.push_screen(
            EditScreen(f"Notes (separate with {notes.SEPARATOR}):", notes.label(r.notes)), done
        )

    def action_edit_tags(self) -> None:
        if self.selected:
            self._bulk_tags()
            return
        self._edit(
            "Tags (comma-separated):",
            lambda r: ", ".join(r.meta.tags),
            lambda key, v: self._update_meta(
                key, lambda m: replace(m, tags=tuple(t.strip() for t in v.split(",") if t.strip()))
            ),
        )

    # --- bulk actions on the selection (#44) -------------------------------

    def action_toggle_select(self) -> None:
        """Add the row under the cursor to the selection, or take it out."""
        r = self._current_row()
        if not r:
            return
        self.selected ^= {r.head.session_id}
        if not self._update_in_place():
            self._render()
        self.action_cursor_down()

    def _targets(self) -> list[Row]:
        """The selection; else, with a filter on, every row shown; else the row under the cursor."""
        if self.selected:
            return [r for r in self.rows if r.head.session_id in self.selected]
        if self.filter.active:
            return list(self.rows)
        r = self._current_row()
        return [r] if r else []

    def _bulk_tags(self) -> None:
        rows = self._targets()

        def done(value: str | None) -> None:
            if value is None:
                return
            words = [w.strip() for w in value.split(",") if w.strip()]
            add = [w.lstrip("+") for w in words if not w.startswith("-")]
            remove = [w[1:] for w in words if w.startswith("-")]
            n = sessions_ops.tag(rows, add, remove)
            self.notify(f"Tags changed on {n} of {len(rows)} conversations")
            self.reload()

        self.push_screen(EditScreen(f"Tags for {len(rows)} (a, b to add; -c to remove):", ""), done)

    def action_trash_sources(self) -> None:
        """Trash conversations that a started session lists in made_from (reversible)."""
        rows = [r for r in self._targets() if not r.meta.combined_from]
        if not rows:
            return
        busy = self._open_ids().union(sid for sid, st in self.live.agents.items() if st.alive)
        if any(busy.intersection(r.ids) for r in rows):
            self.notify("Close the open ones first (x)", severity="warning")
            return
        if not sessions_ops.covered(rows):
            self.notify(
                "No started session lists these in made_from (MCP sessions_create)",
                severity="warning",
            )
            return

        def done(answer: str | None) -> None:
            if answer is None or answer.strip().lower() != "yes":
                return
            dests = sessions_ops.trash(rows)
            self.selected = set()
            self.reload()
            self.notify(
                f"Trashed {len(dests)} conversations ({short_path(str(dests[0].parent), 40)})"
            )

        self.push_screen(EditScreen(f"Trash {len(rows)} conversations? (yes or no)", ""), done)


def _log_crash(exc: BaseException) -> None:
    import traceback

    # The crash log goes in the XDG state dir the package owns, not next to the installed
    # module (read-only under `uv tool install` / a wheel, which would silently drop the trace).
    log = state_home() / APP / "switcher.err"
    try:
        make_dirs(log.parent, state_home())
        with log.open("a", encoding="utf-8") as fh:
            fh.write("".join(traceback.format_exception(exc)) + "\n")
    except OSError:
        pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cabildo workspace",
        description="Two-panel, keyboard-only switcher for Claude Code sessions.",
    )
    parser.add_argument("working_dir", nargs="?", help="default: the current directory")
    parser.add_argument("-a", "--all", action="store_true", help="list every directory")
    parser.add_argument(
        "--forget",
        action="store_true",
        help="stop treating the directory as a workspace (empties its room) and exit",
    )
    parser.add_argument(
        "-G", "--group", action="append", default=[], help="only this group (repeatable)"
    )
    parser.add_argument(
        "-t", "--tag", action="append", default=[], help="only this tag (repeatable)"
    )
    parser.add_argument(
        "-m",
        "--model",
        default="",
        help="Claude's model for every conversation started (over the config file)",
    )
    args = parser.parse_args(argv)
    args.group = [g.strip() for g in args.group]
    args.tag = [t.strip() for t in args.tag]
    if "" in args.group + args.tag:
        parser.error("a group or tag name is empty")
    return args


def _register_workspace(cwd: str) -> None:
    """Make ``cwd`` a workspace on the bus, so the sessions under it share a room.

    Best-effort: the app works without the bus.
    """
    try:
        from cabildo import store

        conn = store.connect()
        try:
            store.register_workspace(conn, cwd)
        finally:
            conn.close()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    started_in = os.getcwd()  # a reload runs the same command line from here
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cwd = os.path.abspath(os.path.expanduser(args.working_dir or os.getcwd()))
    # -G / -t replace the saved filter; without them the last run's filter stays
    only = (
        Filter(groups=tuple(args.group), tags=tuple(args.tag)) if args.group or args.tag else None
    )
    try:
        config = launch.load().with_model(args.model)
    except launch.ConfigError as exc:
        print(f"workspace: {exc}", file=sys.stderr)
        return 2
    if args.forget:
        from cabildo import store

        conn = store.connect()
        try:
            gone = store.forget_workspace(conn, cwd)
        finally:
            conn.close()
        print(f"workspace: {'forgot' if gone else 'was not a workspace:'} {cwd}")
        return 0
    os.chdir(cwd)  # every Claude starts here: --resume looks up the transcript by cwd
    _register_workspace(cwd)
    try:
        moved = tuple(f"Moved {line}" for line in legacy.move_old_files())
    except OSError as exc:
        moved = (f"Could not move the files of an earlier version: {exc}",)
    app = SwitcherApp(
        cwd, notes=moved, all_dirs=True if args.all else None, only=only, config=config
    )
    try:
        app.run(mouse=False)  # keyboard only
    except Exception as exc:
        _log_crash(exc)
        raise
    if app.reload_asked:
        os.chdir(started_in)
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])  # new code, same pid
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
