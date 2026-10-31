"""Claude sessions hosted inside the app: one terminal (textual-tty) per conversation.

A ClaudePane runs Claude in its own pseudo-terminal. It stays mounted while
hidden (the app keeps all panes in a ContentSwitcher), so its Claude keeps
running and switching back is instant. Unmounting a pane closes its
pseudo-terminal, and Claude exits (SIGHUP).

← inside Claude can make a pane show another session. Claude then sets the
terminal title to that session's name; the pane reports every new title
(`TitleSeen`) and the app re-tags it (`session_id`) from a `claude agents`
snapshot taken after the title arrived.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from textual.geometry import Offset
from textual.message import Message
from textual_tty import Terminal

from cabildo.workspace.agents import title_name


@dataclass(frozen=True)
class Target:
    """One conversation to put in the main pane."""

    session_ids: tuple[str, ...]  # a pane showing any of these is this conversation
    session_id: str  # what a new pane shows first ("" for a new conversation)
    command: tuple[str, ...]  # claude --resume <id> | claude attach <id> | claude
    cwd: str | None = None  # where Claude starts; None: where the app runs
    group: str = ""  # a new conversation joins this group once its session id is known


class ClaudePane(Terminal):
    """One hosted Claude."""

    class TitleSeen(Message):
        """Claude set the terminal title (the shown session's name, or agent view)."""

        def __init__(self, pane: ClaudePane, title: str) -> None:
            super().__init__()
            self.pane = pane
            self.title = title

        @property
        def control(self) -> ClaudePane:
            return self.pane

    class Exited(Message):
        """Claude ended (/exit, crash). The pane keeps its last screen."""

        def __init__(self, pane: ClaudePane) -> None:
            super().__init__()
            self.pane = pane

        @property
        def control(self) -> ClaudePane:
            return self.pane

    def __init__(self, target: Target, pane_id: str) -> None:
        command = list(target.command)
        if target.cwd is not None:  # textual-tty starts the process where the app runs
            command = ["sh", "-c", 'cd "$0" && exec "$@"', target.cwd, *command]
        super().__init__(command=command, id=pane_id)
        # not `cwd`: textual-tty's Terminal sets that from the child's OSC 7
        self.launch_cwd = target.cwd
        self.session_id = target.session_id  # changes when ← shows another session
        self.new_group = target.group
        self.title_seen = ""  # the name in the last title, no spinner glyph
        self.title_at = 0.0  # time.monotonic() when it arrived
        self.title_matched = False  # the app has matched it to a session
        self.exited = False
        self.closing = False  # the app is ending it (quit, delete, x)
        self._last_cell: Offset | None = None  # the cursor cell last reported

    # --- the cursor: the real terminal cursor, not a painted cell ------------------
    # textual-tty paints its own cursor (a white block that blinks on its own timer).
    # Plain Claude uses the terminal's real cursor, drawn the way the user has it set,
    # so the pane paints none and puts the real one where Claude's cursor is.

    def _cursor_shown(self) -> bool:
        return False

    def cursor_cell(self) -> Offset | None:
        """Screen cell of Claude's cursor while this pane has the keys and it is visible."""
        if (
            self.exited
            or not self.has_focus
            or self.screen is not self.app.screen
            or not self.board.modes.cursor_visible
        ):
            return None
        region = self.content_region
        x, y = self.board.cursor.x, self.board.cursor.y
        if not (0 <= x < region.width and 0 <= y < region.height):
            return None
        return Offset(region.x + x, region.y + y)

    def _tick(self) -> None:
        if not self.display:
            # hidden: what Claude wrote stays dirty and is painted when shown (#9), but
            # the exit check is in Terminal._tick, so it runs here
            self._check_exit()
            return
        super()._tick()  # paints what Claude wrote since the last frame
        self._report_cursor()

    def _check_exit(self) -> None:
        """Post ``ProcessExited`` once the child is gone (what ``Terminal._tick`` does
        before it paints).
        """
        process = self._process
        code = process.poll() if process is not None and not self._exited else None
        if code is not None:
            self._exited = True
            self._sync = False
            self.post_message(self.ProcessExited(code))

    def _blink(self) -> None:
        if self.display:  # a hidden pane's cursor needs no repaint (#9)
            super()._blink()

    # Textual also runs Terminal's own on_focus / on_blur (it walks the MRO): no super()
    def on_focus(self) -> None:
        self._report_cursor()

    def on_blur(self) -> None:
        self._report_cursor()

    def _report_cursor(self) -> None:
        cell = self.cursor_cell()
        if cell != self._last_cell:  # only changes, not every tick
            self._last_cell = cell
            self.post_message(self.CursorMoved(self, cell))

    class CursorMoved(Message):
        """Claude's cursor moved, showed or hid; the app places the real cursor."""

        def __init__(self, pane: ClaudePane, cell: Offset | None) -> None:
            super().__init__()
            self.pane = pane
            self.cell = cell

        @property
        def control(self) -> ClaudePane:
            return self.pane

    @property
    def pid(self) -> int | None:
        """Claude's process id while it runs."""
        process = self.board.process  # bittty drops it when the child is reaped
        return process.pid if process is not None and not self.exited else None

    def close(self) -> None:
        """End Claude now (the pseudo-terminal closes); the app removes the widget."""
        self.closing = True
        process = self.board.process
        self.board.stop_process()
        if process is not None:  # reap it off the UI thread, so no zombie stays
            threading.Thread(target=process.wait, daemon=True).start()

    def on_terminal_title_changed(self, event: Terminal.TitleChanged) -> None:
        # while Claude works only its spinner glyph changes, several times a second:
        # report a title only when the name in it changed
        name = title_name(event.title)
        if name != self.title_seen:
            self.title_seen = name
            self.title_at = time.monotonic()
            self.title_matched = False
            self.post_message(self.TitleSeen(self, name))

    def on_terminal_process_exited(self, _event: Terminal.ProcessExited) -> None:
        self.exited = True
        self.post_message(self.Exited(self))
