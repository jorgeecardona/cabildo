"""The cabildo workspace — Claude Code sessions and the cabildo bus in one terminal.

Left: the Claude session you work in, running inside this app. Right: the
conversations of one working directory (`a`: with its subfolders, then every directory). Every Claude
you open keeps running while hidden, so switching back is instant. `q` in the
list closes them all; the next start (also after a crash) has them open again.

Run:   cabildo [working-dir]      (default: current directory)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.screen import ModalScreen, Screen
from textual.suggester import SuggestFromList
from textual.timer import Timer
from textual.widgets import ContentSwitcher, DataTable, Input, Label, Static
from textual.worker import get_current_worker

from cabildo import (
    __version__,
    cloud,
    config,
    host_client,
    jupyter,
    names,
    sessions_ops,
    store,
)
from cabildo.workspace import (
    agents,
    attach,
    focus,
    heatmap,
    intents,
    keys,
    launch,
    legacy,
    modeline,
    notes,
    plan,
    rooms,
    routing,
    sessions,
    spend,
    state,
    upstreams,
    usage,
)
from cabildo.workspace.agents import AgentState
from cabildo.workspace.hosting import ClaudePane, Target
from cabildo.workspace.launch import Config
from cabildo.workspace.modeline import (
    SCOPE_HEADERS,
    SCOPE_NAMES,
    Scope,
    fmt_use,
    short_path,
)
from cabildo.workspace.plan import PlanUsage
from cabildo.workspace.sessions import Filter, Meta, Row
from cabildo.workspace.state import OpenState
from cabildo.workspace.usage import (
    Use,
    fmt_cpu,
    fmt_mb,
    fmt_money,
)
from cabildo.workspace.xdgdirs import APP, make_dirs, state_home

CLAUDE = "claude"  # the CLI the panes run; tests point this at a fake
LEADER_TIMEOUT = 1.0  # seconds the hint bar waits for the key after the leader

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


def _tag_hosted(host_id: str, session_id: str, group: str) -> None:
    """Record on the session host which Claude session ``host_id`` runs. Best effort."""
    try:
        host_client.tag_session(host_id, session_id=session_id, group=group or None)
    except (host_client.ClientError, OSError):
        pass


TAG_STYLE = "bold black on dark_cyan"
SCOPES: tuple[Scope, ...] = ("here", "subfolders", "all")  # the order `a` goes through
# the plan usage is fetched once per minute of the clock; every workspace reads the shared
# sample on the same wall-clock beats (:00, :15, :30, :45), so they all change at :00
PLAN_SECONDS = 60.0
PLAN_POLL_SECONDS = 15.0
UPSTREAM_POLL_SECONDS = 5.0  # how often to ask the gateway who serves


INTENT_POLL_SECONDS = 3.0
RUNNER_POLL_SECONDS = 3.0  # how often to look at the Jupyter runners
FOCUS_POLL_SECONDS = 0.5  # how often to look for a sessions_focus request


NO_SPEND_ROW = "-"  # remembered when the login turned out to want no spend row at all


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


def keys_help(keymap: keys.Keymap) -> str:
    """The ``?`` screen, with the global keys this app has bound."""
    lead = keys.display(keymap.leader[0])
    rows = [f"   {keymap.help(a.id).ljust(22)} {a.label}" for a in keys.ACTIONS]
    rows.append(f"   {f'{lead} {lead}'.ljust(22)} send {lead} to Claude")
    return KEYS.format(version=__version__, anywhere="\n".join(rows))


KEYS = """\
 workspace {version}

 Anywhere (also inside Claude): the leader, then one key
{anywhere}

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
   f            filter by group / tag / title  (g:work t:urgent q:trip; empty = all)
   /            search the titles (your label and Claude's): keeps the group / tag filter
   Tab          back to Claude
   j k          move (also the arrows)
   T            CPU, wakeups and memory of each session (like top)
   A            a year of estimated cost, one cell per day
   q  C-q       quit (asks while a Claude runs)
   R            reload: start again on the installed code, keeping the sessions
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


#: The shade of each heatmap level, lightest first. Level 0 is a day with nothing, and it
#: is drawn, not left blank: the grid's shape is the point, and gaps are part of it.
HEAT_COLORS = ("grey30", "#f6c17a", "#e8873e", "#cc4b1f", "#8f2a0c")
HEAT_CELL_W = 2  # a cell and a space, so three-letter month labels do not collide


def heat_text(cal: heatmap.Calendar) -> Text:
    """The calendar as one styled block: months, then a labelled row per weekday."""
    out = Text()
    out.append("    " + heatmap.month_line(cal, HEAT_CELL_W) + "\n", "dim")
    for i, row in enumerate(cal.rows()):
        out.append(f"{heatmap.ROW_LABELS[i]:<4}", "dim")
        for cell in row:
            out.append(cell.char + " ", HEAT_COLORS[cell.level])
        out.append("\n")
    return out


def heat_legend(cal: heatmap.Calendar) -> Text:
    """``$7,752 over 20 days · Less ▪▪▪▪▪ More``, under the grid."""
    days = sum(1 for week in cal.weeks for cell in week if cell.value)
    out = Text(f"{fmt_money(cal.total)} over {days} day{'' if days == 1 else 's'}", "dim")
    out.append("     Less ", "dim")
    for color in HEAT_COLORS:
        out.append(heatmap.CELL + " ", color)
    out.append("More", "dim")
    return out


class HeatScreen(ModalScreen[None]):
    """`A`: a year of estimated cost, one cell per day, like any contribution graph.

    Dollars rather than tokens: tokens of different models are not the same amount, so a
    day on Haiku and a day on Fable would otherwise shade alike.
    """

    BINDINGS = [Binding("escape,q,A", "close", "Close")]

    def __init__(self, cal: heatmap.Calendar) -> None:
        super().__init__()
        self._cal = cal

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            yield Static(
                "Estimated cost per day on this machine · q closes",
                id="top-title",
            )
            yield Static(heat_text(self._cal), id="heat-grid")
            yield Static(heat_legend(self._cal), id="heat-legend")

    def action_close(self) -> None:
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


class RunnersScreen(ModalScreen[None]):
    """`J`: the Jupyter runners with the CPU and memory of each one's server and kernels.
    `k` stops the selected one, `p` pauses it (or resumes it), `o` opens it in a browser.
    """

    BINDINGS = [
        Binding("escape,q,J", "close", "Close"),
        Binding("k", "act('stop')", "Stop"),
        Binding("p", "act('pause')", "Pause / resume"),
        Binding("r", "act('restart')", "Restart kernel"),
        Binding("i", "act('interrupt')", "Interrupt"),
        Binding("o,enter", "act('open')", "Open"),
    ]

    def __init__(self, lines: list[tuple[str, tuple[Text, str, str, str, str]]]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            yield Static(
                "Jupyter runners · CPU: % of one core, server and kernels · k stops · "
                "p pauses or resumes · r restarts the kernel · i interrupts · o opens in the browser · q closes",
                id="top-title",
            )
            yield DataTable(id="top-table", cursor_type="row", zebra_stripes=False)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for label, width in (
            ("Runner", 16),
            ("State", 9),
            ("Directory", 40),
            ("CPU", 7),
            ("Mem", 7),
        ):
            table.add_column(label, width=width)
        self.show(self._lines)
        table.focus()

    def show(self, lines: list[tuple[str, tuple[Text, str, str, str, str]]]) -> None:
        """Redraw the rows, keeping the cursor on the same row number."""
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        for name, cells in lines:
            table.add_row(*cells, key=name)
        self._lines = lines
        if lines:
            table.move_cursor(row=min(row, len(lines) - 1))
        else:
            self.query_one("#top-title", Static).update(
                "No runner yet: `cabildo jupyter start NAME --cwd DIR`, or an agent's jupyter_start"
            )

    def action_act(self, action: str) -> None:
        table = self.query_one(DataTable)
        app = self.app
        if table.row_count and isinstance(app, SwitcherApp):
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            app.runner_do(action, str(key.value))

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
    Screen { layers: base hint; }
    #leader-hint { layer: hint; dock: bottom; height: 1; width: 1fr; background: $panel; color: $accent; }
    #modeline {
        dock: bottom;
        height: auto;  /* groups of lines, a rule between each */
        background: $foreground;
        color: $background;
        padding: 0 1;
    }
    EditScreen { background: transparent; }
    KeysScreen { align: center middle; background: transparent; }
    TopScreen, RunnersScreen { align: center middle; background: transparent; }
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
        Binding(keys.LEADER, "leader", "Leader", priority=True, show=False, id="workspace.leader"),
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
        Binding("slash", "search", "Search"),
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
        Binding("A", "activity", "Activity"),
        Binding("J", "runners", "Runners"),
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
        self._focus_dir: Path | None = None  # where sessions_focus requests arrive
        self.selected: set[str] = set()  # `space`: head session ids the bulk actions act on
        self.scope: Scope = "here"  # `a` rotates it: here, subfolders, all
        self.filter = Filter()  # `f`: the list shows only these groups / tags / titles
        self.reading = False  # a scan for a new scope is on its way
        self.plan_usage: PlanUsage | None = None  # the subscription's limits, once fetched
        self.plan_pace: plan.PlanPace | None = None  # how fast they fill
        self.upstreams: upstreams.View | None = None  # the gateway, when sessions use it
        self._upstream_reader = upstreams.Reader()
        self.spend: spend.Report | None = None  # consumed tokens and cost, when shown
        self.spend_label = ""  # "" while the mode line shows no spend row
        self.spend_mode = "never"  # [workspace].spend; read at mount
        self._auth_seen = ""  # the row's label once the login has been read, on the worker
        self._spend_busy = False  # a spend refresh is in flight
        self._cwd_key = cloud.project_key(cwd)  # what the intents of "here" are matched on
        self.intents: tuple[intents.Intent, ...] = ()  # the push queue and its latest results
        self._intents_seen = False  # the bus has been read once: show the group, even if empty
        self.runners: tuple[jupyter.RunnerRow, ...] = ()  # the Jupyter runners, polled
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
        self._leader_timer: Timer | None = None  # set while the leader waits for its key
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
        hint = Static("", id="leader-hint")
        hint.display = False
        yield hint

    def on_mount(self) -> None:
        for note in self._notes:
            self.notify(note, timeout=12)
        self._last_run = state.load(self.cwd)
        last = self._last_run
        self.scope = "all" if self._start_all_dirs else "here"  # never the last run's
        self.filter = last.filter if self._start_filter is None else self._start_filter
        self.reading = True
        self._render()  # empty table and mode-line until the scan returns
        # Before anything else can open a pane: the Claudes a reload handed over. They
        # are already running, so `_restore` finds a shown pane and opens nothing again.
        self.call_later(self._remount_handed)
        self.reload()
        self.query_one(DataTable).focus()
        self.refresh_live(SHARE_SECONDS)
        self.set_interval(POLL_SECONDS, lambda: self.refresh_live(SHARE_SECONDS))
        if usage.available():
            self.set_interval(SAMPLE_SECONDS, self._sample_tick)
        self._plan_tick()
        self._upstreams_tick()
        self.set_interval(UPSTREAM_POLL_SECONDS, self._upstreams_tick)
        self._spend_start()
        self._intents_tick()
        self.set_interval(INTENT_POLL_SECONDS, self._intents_tick)
        self._runners_tick()
        self.set_interval(RUNNER_POLL_SECONDS, self._runners_tick)
        self._focus_dir = focus.register()
        self.set_interval(FOCUS_POLL_SECONDS, self._focus_tick)

    def on_unmount(self) -> None:
        if self._focus_dir is not None:
            focus.unregister(self._focus_dir)

    async def _focus_tick(self) -> None:
        """Show the conversation a ``sessions_focus`` request names, then answer it."""
        folder = self._focus_dir
        request = focus.take(folder) if folder else None
        if folder is None or request is None:
            return
        found = sessions_ops.find(self.rows, request.name)
        if len(found) > 1:
            titles = ", ".join(repr(r.display_title) for r in found[:5])
            message = f"{len(found)} sessions match {request.name!r}: {titles}"
            focus.answer(folder, request, ok=False, message=message)
            return
        if not found:
            note = " (a filter is on)" if self.filter.active else ""
            message = f"no session {request.name!r} in this workspace's list{note}"
            focus.answer(folder, request, ok=False, message=message)
            return
        row = found[0]
        await self._open(row)
        self._restore_cursor(row.head.session_id)
        focus.answer(folder, request, ok=True, message=f"showing {row.display_title!r}")

    def _runners_tick(self) -> None:
        self.run_worker(self._runners_fetch, thread=True, group="runners", exclusive=True)

    def _runners_fetch(self) -> None:
        self._to_ui(self._runners_got, tuple(jupyter.statuses()))

    def _runners_got(self, found: tuple[jupyter.RunnerRow, ...]) -> None:
        if found == self.runners:
            return
        self.runners = found
        screen = self.screen
        if isinstance(screen, RunnersScreen):
            screen.show(self._runner_lines())
        self._modeline()

    def _runner_lines(self) -> list[tuple[str, tuple[Text, str, str, str, str]]]:
        """Rows of the runners screen: (name, (runner, state, directory, CPU, memory))."""
        lines = []
        for r in self.runners:
            use = self.uses.get(r.pid) if r.pid else None
            state = "paused" if r.paused else "running" if r.running else "stopped"
            style = "dim" if not r.running or r.paused else "bold"
            cpu = fmt_cpu(use.cpu) if use else ""
            mem = fmt_mb(use.mem_mb) if use else ""
            cwd = short_path(r.cwd, 40)
            lines.append((r.name, (Text(r.name, style=style), state, cwd, cpu, mem)))
        return lines

    def runner_do(self, action: str, name: str) -> None:
        """`k`, `p`, `o` on the runners screen: stop, pause or resume, open in the browser.
        Runs on a worker (stopping waits for the process), then reads the runners again.
        """

        def work() -> None:
            try:
                if action == "stop":
                    jupyter.stop(name)
                elif action == "restart":
                    jupyter.restart(name)
                elif action == "interrupt":
                    jupyter.interrupt(name)
                elif action == "pause":
                    row = next((r for r in jupyter.statuses() if r.name == name), None)
                    (jupyter.resume if row and row.paused else jupyter.pause)(name)
                elif action == "open":
                    jupyter.open_link(name, launch=True)
            except jupyter.RunnerError as exc:
                self._to_ui(self.notify, f"{name}: {exc}", severity="warning")
            self._to_ui(self._runners_tick)

        self.run_worker(work, thread=True, group="runner-action")

    def action_runners(self) -> None:
        """`J`: the Jupyter runners, with `k` stop, `p` pause or resume, `o` open."""
        self.push_screen(RunnersScreen(self._runner_lines()))
        self._runners_tick()
        self.call_later(lambda: self._sample_tick(force=True))

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

    def _upstreams_tick(self) -> None:
        self.run_worker(self._upstreams_fetch, thread=True, group="upstreams", exclusive=True)

    def _upstreams_fetch(self) -> None:
        self._to_ui(self._upstreams_got, self._upstream_reader.read())

    def _upstreams_got(self, found: upstreams.View | None) -> None:
        if found != self.upstreams:
            self.upstreams = found
            self._modeline()

    def _spend_start(self) -> None:
        """Start the spend refresh, unless the config turns the row off.

        Which login this is, is decided on the worker thread, not here: reading the
        credentials means a `security` subprocess on macOS, and app start is not the
        place to wait on one.
        """
        self.spend_mode = config.spend_mode(self.cwd)
        if self.spend_mode == "never":
            return
        self._spend_tick()
        self.set_interval(spend.REFRESH_SECONDS, self._spend_tick)

    def _spend_tick(self) -> None:
        """Start a refresh unless one is in flight.

        Not `exclusive=True`: that cancels the running worker, and a cancelled worker is
        an error to anything waiting on the worker manager. Switching panes can fire this
        while the periodic tick runs, so the two must not race. The one in flight already
        reads the pane that is shown when it gets there, and the next tick follows soon.
        """
        if self._spend_busy:
            return
        self._spend_busy = True
        self.run_worker(self._spend_fetch, thread=True, group="spend")

    def _spend_fetch(self) -> None:
        """Off the UI thread: which login this is, and what it consumed.

        The login is read once and remembered -- it does not change while the app runs,
        and on macOS reading it is a subprocess. Only this worker touches `_auth_seen`,
        and it is exclusive in its group, so there is no second thread to race with.
        """
        if not self._auth_seen:
            self._auth_seen = self._spend_heading() or NO_SPEND_ROW
        label = "" if self._auth_seen == NO_SPEND_ROW else self._auth_seen
        try:
            found = spend.load(time.time(), self.cwd, self._shown_ids()) if label else None
        finally:
            self._spend_busy = False
        self._to_ui(self._spend_got, found, label)

    def _spend_heading(self) -> str:
        """The row's label, or "" when there is to be no row."""
        return spend.heading(self.spend_mode)

    def _shown_ids(self) -> tuple[str, ...]:
        """Every session id of the conversation in the main pane, for its own spend row.

        Every id, not the current one: a conversation resumed twice is three session ids
        and one piece of work, and charging only the latest would read as a cheap session
        every time one is resumed.
        """
        shown = self.shown_session()
        if not shown:
            return ()
        row = next((r for r in self.rows if shown in r.ids), None)
        return tuple(row.ids) if row else (shown,)

    def _spend_got(self, found: spend.Report | None, label: str = "") -> None:
        if (found, label) == (self.spend, self.spend_label) or found is None:
            return
        self.spend, self.spend_label = found, label
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
            self._tag_host(pane)
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
        """Title (+ tags), then where it works (cwd · branch), then its agent name and rooms
        if it is on the bus, dimmed.
        """
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
        agent = f"@{r.rooms.agent}" if r.rooms.agent else ""
        room_text = " ".join(part for part in (agent, rooms.label(r.rooms)) if part)
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
        """Groups of lines, a rule between each (`modeline.compose`). The first says what
        the main pane shows: its title, its cabildo agent name and its directory (or the key
        hints). The CPU and memory table ends with this app's own row.
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
        if pane and pane.exited:
            top = f"{top} (ended)"
        if self.reading:
            bottom = "reading transcripts…"
        # the cabildo name other agents address this one by (deterministic from the id)
        agent = f"@ {names.robot_name(shown)}" if shown else ""
        ids = (shown, *row.ids) if shown and row else (shown,) if shown else ()
        selected = modeline.Selected(
            ids,
            row,
            self._row_use(row) if row else None,
            self._row_first_mb(row) if row else None,
        )
        listed = None
        if self.intents or (self._intents_seen and store.db_path().exists()):
            listed = intents.in_scope(self.intents, self.scope, self.cwd, self._cwd_key)
        readings = modeline.Readings(
            self.plan_usage,
            self.plan_pace,
            self.upstreams,
            self.spend,
            self.spend_label,
            listed,
            self.runners,
            self.uses,
        )
        machine = None
        if usage.available():
            everyone, me = self._usage_lines()
            running = set(self.uses) - {os.getpid()}
            same = bool(row and running and running <= self._row_pids(row))  # only this one
            machine = modeline.Machine("" if same else everyone, me)
        groups = modeline.compose(
            modeline.Heading(top, agent, bottom), selected, readings, machine, width
        )
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
                self._tag_host(pane)
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
        roots |= {r.pid: True for r in self.runners if r.pid and r.running}
        roots[os.getpid()] = False
        return roots

    def _sample_tick(self, force: bool = False) -> None:
        """Sample only while someone can see it: every SAMPLE_SECONDS while the list
        shows (an interval timer), every TOP_SAMPLE_SECONDS after the last sample while
        the top view is open (`_sampled` sets the next timer), never while hidden.
        """
        top = isinstance(self.screen, (TopScreen, RunnersScreen))
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
        screen = self.screen
        if isinstance(screen, (TopScreen, RunnersScreen)):
            if isinstance(screen, TopScreen):
                screen.show(self._top_lines())
            else:
                screen.show(self._runner_lines())
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

    def action_activity(self) -> None:
        """`A`: the year's token use as a calendar. Reads the DB on a worker, since a
        cold transcript scan is seconds and the key press must not wait on it.
        """
        self.run_worker(self._activity_fetch, thread=True, group="activity", exclusive=True)

    def _activity_fetch(self) -> None:
        self._to_ui(self._activity_got, spend.calendar(dt.date.today()))

    def _activity_got(self, cal: heatmap.Calendar | None) -> None:
        if cal is None:
            self.notify("No token history yet: the bus DB has nothing to draw", severity="warning")
            return
        self.push_screen(HeatScreen(cal))

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
        """How to open a conversation (`routing.route`) as a pane's target; None when it
        cannot be resumed because its directory is gone.
        """
        found = routing.route(
            r,
            self.cwd,
            self.live.agents.get(r.head.session_id),
            self.live.loaded,
            host_client.list_sessions,
            self.config,
            CLAUDE,
        )
        if found is None:
            return None
        owned = found.host
        target = Target(
            session_ids=r.ids,
            session_id=found.session_id,
            command=host_client.attach_command(owned.id) if owned else found.command,
            cwd=found.cwd,
            host_id=owned.id if owned else "",
            host_pid=owned.pid if owned else 0,
        )
        return self._hosted(target) if found.kind == "resume" else target

    def _hosted(self, target: Target) -> Target:
        """With ``[sessions] owner = "host"``, run the target's Claude in the session host.

        Returns a target whose command attaches to it. Anything that goes wrong keeps the
        target as it was, so Claude runs in this app, as it does without the option.
        """
        if self.config.owner != "host" or not target.command or target.host_id:
            return target
        if target.command[:2] == (CLAUDE, "attach"):  # a background session: nothing to own
            return target
        size = self.query_one("#main").size
        try:
            record = host_client.start_session(
                target.command,
                cwd=target.cwd or os.getcwd(),
                rows=max(2, size.height),
                cols=max(2, size.width),
                group=target.group,
                session_id=target.session_id,
            )
        except (host_client.ClientError, OSError) as exc:
            self.notify(f"Session host: {exc}. Claude runs in this app.", severity="warning")
            return target
        return replace(
            target,
            command=host_client.attach_command(record.id),
            host_id=record.id,
            host_pid=record.pid,
        )

    def _tag_host(self, pane: ClaudePane) -> None:
        """Tell the session host which Claude session a hosted pane shows, so any viewer finds it."""
        if not (pane.host_id and pane.session_id):
            return
        threading.Thread(
            target=_tag_hosted,
            args=(pane.host_id, pane.session_id, pane.new_group),
            daemon=True,
        ).start()

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
        target = self._hosted(Target(session_ids=(), session_id="", command=command, group=group))
        self._show(await self._mount(target))
        self.set_timer(NEW_POLL_SECONDS, self.refresh_live)

    async def _mount(self, target: Target, handed: attach.Handed | None = None) -> ClaudePane:
        self._pane_seq += 1
        pane = ClaudePane(target, f"pane-{self._pane_seq}", handed)
        await self.query_one("#main", ContentSwitcher).mount(pane)
        return pane

    async def _remount_handed(self) -> None:
        """Rebuild the panes a reload handed over, before anything else opens one.

        A descriptor that cannot be rebuilt is closed and its Claude hung up: an inherited
        pseudo-terminal nobody reads leaves Claude blocked on a full buffer, which is
        worse than the hangup a plain quit would have given it.
        """
        for handed in attach.take():
            owned_by_host = handed.fd < 0  # the host took it: a new viewer, nothing inherited
            target = Target(
                session_ids=(handed.session_id,) if handed.session_id else (),
                session_id=handed.session_id,
                command=host_client.attach_command(handed.host_id) if owned_by_host else (),
                cwd=handed.cwd or None,
                group=handed.group,
                host_id=handed.host_id,
                host_pid=handed.host_pid,
            )
            try:
                pane = await self._mount(target, None if owned_by_host else handed)
            except Exception:  # one bad pane must not cost the others
                if not owned_by_host:
                    attach.discard(handed.fd)
                    attach.hangup(handed.pid)
                continue
            pane.title_seen = handed.title
            self._show(pane, focus=True)
            if not owned_by_host:
                pane.repaint_child()

    def _show(self, pane: ClaudePane, focus: bool = True) -> None:
        self.query_one("#main", ContentSwitcher).current = pane.id
        if focus:
            pane.focus()
        self._refresh_marks()
        self.refresh_live()  # a new Claude: its state shows without waiting a poll
        if self.spend_mode != "never":
            self._spend_tick()  # the session column is about the pane that just appeared
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
        """`f`: show only some groups / tags / titles, as `g:work t:urgent q:trip`; empty shows all."""
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

    def action_search(self) -> None:
        """`/`: show only the conversations whose title has every word typed; empty clears."""

        def done(value: str | None) -> None:
            if value is None:
                return
            words = tuple(value.split())
            if words != self.filter.titles:
                self.filter = replace(self.filter, titles=words)
                self.reading = True
                self._modeline()
                self.reload()
                self._save_state()

        self.push_screen(EditScreen("Search titles:", " ".join(self.filter.titles)), done)

    def action_leader(self) -> None:
        """The leader: a hint bar over the bottom line; the next key starts an action.

        No screen is pushed: a modal screen hides the Claude pane behind it.
        """
        hint = self.query_one("#leader-hint", Static)
        hint.update(self.config.keys.hint())
        hint.display = True
        self._leader_timer = self.set_timer(LEADER_TIMEOUT, self._leader_done)

    @property
    def leader_pending(self) -> bool:
        """The leader was pressed and the next key is not in yet."""
        return self._leader_timer is not None

    def _leader_done(self) -> None:
        if self._leader_timer is not None:
            self._leader_timer.stop()
            self._leader_timer = None
        self.query_one("#leader-hint", Static).display = False

    async def on_event(self, event: events.Event) -> None:
        # the key after the leader goes to the leader, before bindings and the focused pane
        if isinstance(event, events.Key) and not event.is_forwarded and self.leader_pending:
            self._leader_done()
            await self._after_leader(event.key)
            return
        await super().on_event(event)

    async def _after_leader(self, key: str) -> None:
        if key == "escape":
            return
        if key in self.config.keys.leader:
            pane = self._shown_pane()
            if pane:
                # the raw byte: bittty turns ctrl + a letter into a control code, not ctrl + ]
                pane.board.display.input_key("\x1d")
            return
        action = self.config.keys.action_for(key)
        if action is None:
            self.notify(f"{keys.display(key)} is not bound after the leader", timeout=3)
            return
        await self.run_action(action.method)

    def action_palette(self) -> None:
        """The command palette, for the actions that have no key of their own."""
        self.push_screen(CommandPalette(id="--command-palette"))

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield SystemCommand(
            "Reload",
            "Start again on the installed code, keeping the sessions",
            self.action_reload,
        )
        yield SystemCommand("Top", "CPU, wakeups and memory of each session", self.action_top)
        yield SystemCommand("Scope", "This directory / subfolders / all", self.action_toggle_all)
        yield SystemCommand("Filter", "Show only some groups / tags / titles", self.action_filter)
        yield SystemCommand("Search", "Show only titles with these words", self.action_search)
        yield SystemCommand("Keys", "Show all keys", self.action_toggle_keys)

    def action_toggle_keys(self) -> None:
        self.push_screen(KeysScreen(keys_help(self.config.keys)))

    def action_quit(self) -> None:  # type: ignore[override]
        """Close every Claude, then the app; asks first while any Claude runs."""
        self._ask_quit("close and quit?")

    def action_reload(self) -> None:
        """`R`: start again on the code installed now, keeping every Claude running.

        No question asked, because nothing is ended: the process re-execs itself, and the
        panes hand their pseudo-terminals across (`attach.py`). What is lost is each
        pane's scrollback, which lived in the program being replaced; Claude redraws its
        current screen on the other side.
        """
        self.reload_asked = True
        self._quit()

    def _ask_quit(self, question: str) -> None:
        """Ask before ending running Claudes. Only quitting asks: a reload ends nothing."""

        def go() -> None:
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
        if self.reload_asked:
            # A reload re-execs this very process, which keeps its children and its open
            # descriptors. So the panes are handed over rather than closed, and the
            # Claudes never learn the program around them was replaced.
            handed = [h for pane in self._panes() if (h := pane.detach()) is not None]
            if self.config.owner == "host":  # the host takes the Claudes this app owns
                handed = [self._to_host(h) for h in handed]
            attach.put(handed)
        else:
            for pane in self._panes():
                pane.close(end_session=False)  # a hosted Claude keeps running without its viewer
        self.exit()

    def _to_host(self, handed: attach.Handed) -> attach.Handed:
        """Give a detached pane's Claude to the session host; the pane then attaches to it.

        The host receives its own copy of the pseudo-terminal descriptor, so this one is
        closed without hanging Claude up. If the host cannot take it, the pane is handed over
        through ``exec`` as it is without the option.
        """
        if handed.host_id:
            return handed
        try:
            record = host_client.adopt_session(
                handed.fd,
                handed.pid,
                cwd=handed.cwd,
                rows=handed.rows,
                cols=handed.cols,
                group=handed.group,
                session_id=handed.session_id,
            )
        except (host_client.ClientError, OSError):
            return handed
        attach.discard(handed.fd)
        return replace(handed, fd=-1, host_id=record.id, host_pid=handed.pid)

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
    # Not reloading: any pseudo-terminal still written down was handed over by a reload
    # that then failed to start. Close it, so its Claude is hung up rather than left
    # blocked on a terminal nobody reads.
    for handed in attach.take():
        attach.discard(handed.fd)
        attach.hangup(handed.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
