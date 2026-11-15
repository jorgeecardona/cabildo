"""Claude hosted inside the app: panes, switching, keys, ← retagging, exit, quit.

The panes run tests/conftest.py's fake `claude` (prints its args, sets the title,
echoes keys) in a real pseudo-terminal; the app runs headless through the pilot.
"""

from __future__ import annotations

import fcntl
import os

import pytest
from lines import PROJECT, reply, user
from pilot import background, drive, settle, wait_until
from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual_tty import Terminal

import cabildo.workspace.app as app_module
from cabildo.workspace import agents, attach, sessions, state
from cabildo.workspace.app import SwitcherApp
from cabildo.workspace.hosting import ClaudePane
from cabildo.workspace.launch import Config, Launch
from cabildo.workspace.state import OpenState


@pytest.fixture
def three(transcript, fake_claude) -> None:
    for i, sid in enumerate(["s0", "s1", "s2"]):
        transcript(sid, user(f"session {sid}"), mtime=3_000 - i)  # rows in this order


def screen(pane: ClaudePane) -> str:
    return pane.board.capture_text()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


TOP = ("k",) * 9  # up to the first row


async def open_row(app: SwitcherApp, pilot, row: int) -> ClaudePane:
    """Alt-s to the list, go to the row, Enter; wait until its Claude printed."""
    await pilot.press("alt+s", *TOP, *["j"] * row, "enter")
    await settle(app, pilot)
    pane = app._shown_pane()
    assert pane is not None
    assert await wait_until(pilot, lambda: "FAKE" in screen(pane))
    return pane


def test_enter_starts_claude_in_a_pane_and_focuses_it(three, monkeypatch) -> None:
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        seen.update(
            text=screen(pane),
            focused=app.focused is pane,
            shown=app.shown_session(),
            mark=app.query_one(DataTable).get_row_at(0)[0],
        )

    drive(monkeypatch, scenario)
    assert "FAKE --resume s0" in seen["text"]  # the fake prints its arguments
    assert seen["focused"] and seen["shown"] == "s0" and seen["mark"] == "▶"


def test_switching_back_reuses_the_running_claude(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        first = await open_row(app, pilot, 0)
        pid = first.pid
        second = await open_row(app, pilot, 1)
        again = await open_row(app, pilot, 0)
        seen.extend([again is first, again.pid == pid, alive(second.pid), len(app._panes())])

    drive(monkeypatch, scenario)
    assert seen == [True, True, True, 2]  # same pane and process; the other kept running


def test_c_r_restarts_the_claude_on_the_same_conversation(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        first = await open_row(app, pilot, 0)
        pid = first.pid
        await pilot.press("alt+s", "ctrl+r")
        assert await wait_until(pilot, lambda: not alive(pid))
        assert await wait_until(
            pilot, lambda: (p := app._shown_pane()) is not None and p.pid != pid
        )
        again = app._shown_pane()
        assert await wait_until(pilot, lambda: "FAKE" in screen(again))
        seen.extend([again is not first, "FAKE --resume s0" in screen(again), len(app._panes())])

    drive(monkeypatch, scenario)
    assert seen == [True, True, 1]  # the old Claude ended, a new one resumed s0


def test_c_r_on_a_row_not_open_starts_it(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press(*TOP, "ctrl+r")
        await settle(app, pilot)
        pane = app._shown_pane()
        assert pane is not None
        assert await wait_until(pilot, lambda: "FAKE" in screen(pane))
        seen.append(app.shown_session())

    drive(monkeypatch, scenario)
    assert seen == ["s0"]


def test_keys_go_to_claude(three, monkeypatch) -> None:
    seen: list[str] = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        await pilot.press("x", "y", "z", "q", "j")  # q / j are list keys: here they are text
        await wait_until(pilot, lambda: "xyzqj" in screen(pane))
        seen.append(screen(pane))
        assert app.is_running

    drive(monkeypatch, scenario)
    assert "xyzqj" in seen[0]


def test_alt_z_hides_and_shows_the_list_and_alt_s_focuses_it(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        side = app.query_one("#side")
        await pilot.press("alt+z")  # from inside Claude
        seen.append((side.display, app.focused is pane))
        await pilot.press("alt+z")
        seen.append((side.display, app.focused is pane))
        await pilot.press("alt+z", "alt+s")
        seen.append((side.display, isinstance(app.focused, DataTable)))
        await pilot.press("tab")  # list -> Claude
        seen.append((side.display, app.focused is pane))

    drive(monkeypatch, scenario)
    assert seen == [(False, True), (True, True), (True, True), (True, True)]


def test_ctrl_q_inside_claude_does_not_quit(three, monkeypatch) -> None:
    async def scenario(app, pilot) -> None:
        await open_row(app, pilot, 0)
        await pilot.press("ctrl+q")
        await settle(app, pilot)
        assert app.is_running

    drive(monkeypatch, scenario)


def catch_exit(app: SwitcherApp) -> list[bool]:
    """Record app.exit() instead of leaving the pilot."""
    calls: list[bool] = []
    app.exit = lambda *a, **k: calls.append(True)  # type: ignore[method-assign]
    return calls


@pytest.mark.parametrize(("answer", "quits"), [("yes", True), ("no", False)])
def test_q_asks_while_claude_runs(three, monkeypatch, answer: str, quits: bool) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        pids = [(await open_row(app, pilot, 0)).pid, (await open_row(app, pilot, 1)).pid]
        exits = catch_exit(app)
        await pilot.press("alt+s", "q", *answer, "enter")
        await settle(app, pilot)
        seen.extend([bool(exits), agents.wait_exit(pids, timeout=3 if quits else 0.3)])

    drive(monkeypatch, scenario)
    assert seen == [quits, quits]  # yes: quit and every Claude ended; no: nothing


def test_q_quits_at_once_with_no_claude_running(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        exits = catch_exit(app)
        await pilot.press("q")
        await settle(app, pilot)
        seen.append(bool(exits))

    drive(monkeypatch, scenario)
    assert seen == [True]


def test_an_ended_claude_is_started_again(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        old = pane.pid
        os.kill(old, 9)
        assert await wait_until(pilot, lambda: pane.exited)
        await settle(app, pilot)
        seen.append(isinstance(app.focused, DataTable))  # keys back to the list
        again = await open_row(app, pilot, 0)
        seen.extend([again.pid != old, alive(again.pid), len(app._panes())])

    drive(monkeypatch, scenario)
    assert seen == [True, True, True, 1]


def test_left_arrow_switch_retags_the_pane_and_moves_the_cursor(three, monkeypatch) -> None:
    live = {"s2": background("s2", name="bravo exact reply test")}
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        # what Claude does after ← and picking s2 in agent view: set the title to its name
        pane.post_message(Terminal.TitleChanged("bravo exact reply test", ""))
        await settle(app, pilot)
        table = app.query_one(DataTable)
        seen.extend(
            [pane.session_id, app.shown_session(), table.cursor_row, table.get_row_at(2)[0]]
        )

    drive(monkeypatch, scenario, live=live)
    assert seen == ["s2", "s2", 2, "▶"]


def test_the_fake_title_reaches_the_pane(three, monkeypatch) -> None:
    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        assert await wait_until(pilot, lambda: pane.title_seen == "fake-s0")

    drive(monkeypatch, scenario)


def test_delete_closes_the_pane(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        pid = pane.pid
        await pilot.press("alt+s", *TOP, "ctrl+d", "y", "e", "s", "enter")
        await settle(app, pilot)
        seen.extend(
            [
                agents.wait_exit([pid], timeout=3),
                app._panes(),
                app.shown_session(),
                [r.head.session_id for r in app.rows],
            ]
        )

    drive(monkeypatch, scenario)
    assert seen == [True, [], None, ["s1", "s2"]]


def test_main_starts_in_the_list_directory(monkeypatch, tmp_path) -> None:
    seen: list[str] = []
    monkeypatch.setattr(app_module.SwitcherApp, "run", lambda self, **_k: seen.append(os.getcwd()))
    monkeypatch.setattr(app_module.sys, "argv", ["app.py", str(tmp_path)])
    monkeypatch.chdir("/")
    assert app_module.main() == 0
    assert seen == [str(tmp_path)]


#: These read /proc directly, or assert on the per-process CPU/memory column, which
#: `SwitcherApp` itself reports as "CPU and memory per session need Linux /proc". Off Linux
#: there is nothing to read, so they are skipped rather than failing every run on a Mac.
_needs_proc = pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")


@_needs_proc
def test_no_claude_is_left_after_the_tests_app_closes(three, monkeypatch) -> None:
    pids: list[int] = []

    async def scenario(app, pilot) -> None:
        pids.append((await open_row(app, pilot, 0)).pid)

    drive(monkeypatch, scenario)  # the app closes without `q`
    assert agents.wait_exit(pids, timeout=3)


def test_focus_is_visible(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        side, table = app.query_one("#side"), app.query_one(DataTable)

        def look() -> tuple:
            return (side.styles.border_left[1], table.get_component_rich_style("datatable--cursor"))

        await pilot.pause()
        on_list = look()
        await open_row(app, pilot, 0)  # Claude has the keys now
        await pilot.pause()
        on_claude = look()
        seen.extend([on_list[0] != on_claude[0], on_list[1] != on_claude[1]])

    drive(monkeypatch, scenario)
    assert seen == [True, True]  # the list border and its cursor both change


def test_question_mark_shows_the_keys_and_any_key_closes_them(three, monkeypatch) -> None:
    from cabildo.workspace.app import KeysScreen

    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("question_mark")
        await pilot.pause()
        seen.append(isinstance(app.screen, KeysScreen))
        await pilot.press("x")
        await pilot.pause()
        seen.extend([isinstance(app.screen, KeysScreen), app.is_running])

    drive(monkeypatch, scenario)
    assert seen == [True, False, True]


def test_the_real_cursor_follows_claudes_cursor(three, monkeypatch) -> None:
    from textual.geometry import Offset

    from cabildo.workspace.app import KeysScreen

    seen: list = []

    def expected(pane: ClaudePane) -> Offset:
        r = pane.content_region
        return Offset(r.x + pane.board.cursor.x, r.y + pane.board.cursor.y)

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        await pilot.press("a", "b", "c")
        await wait_until(pilot, lambda: "abc" in screen(pane))
        await wait_until(pilot, lambda: app.real_cursor == expected(pane))
        seen.append(("in Claude", app.real_cursor == expected(pane), pane._cursor_shown()))
        await pilot.press("alt+s")  # the list has the keys
        await wait_until(pilot, lambda: app.real_cursor is None)
        seen.append(("in list", app.real_cursor))
        await pilot.press("tab")  # back to Claude
        await wait_until(pilot, lambda: app.real_cursor is not None)
        seen.append(("back", app.real_cursor == expected(pane)))
        await pilot.press("alt+s", "question_mark")  # a screen on top
        await pilot.pause()
        seen.append(("keys screen", isinstance(app.screen, KeysScreen), app.real_cursor))

    drive(monkeypatch, scenario)
    assert seen == [
        ("in Claude", True, False),
        ("in list", None),
        ("back", True),
        ("keys screen", True, None),
    ]


# --- n: a new conversation --------------------------------------------------------------


def interactive(sid: str, pid: int | None, name: str = "proj-d1") -> agents.AgentState:
    return agents.AgentState(
        session_id=sid,
        agent_id=sid[:8],
        kind="interactive",
        name=name,
        cwd="/work/proj",
        status="idle",
        state="",
        pid=pid,
    )


def modeline(app: SwitcherApp) -> list[str]:
    return str(app.query_one("#modeline").render()).split("\n")


def groups(app: SwitcherApp) -> list[list[str]]:
    """The mode line's groups of lines, split at the rules between them."""
    out: list[list[str]] = [[]]
    for line in modeline(app):
        if line and set(line) == {"─"}:
            out.append([])
        else:
            out[-1].append(line)
    return out


def test_n_starts_a_new_conversation_and_follows_it_into_the_list(
    three, transcript, monkeypatch
) -> None:
    live: dict = {}
    seen: list = []

    async def scenario(app, pilot) -> None:
        table = app.query_one(DataTable)
        await pilot.press("j", "j", "ctrl+n")  # cursor on s2, then C-n
        await settle(app, pilot)
        pane = app._shown_pane()
        assert pane is not None
        assert await wait_until(pilot, lambda: "FAKE" in screen(pane))
        seen.append(
            (
                "started",
                screen(pane).splitlines()[0].rstrip(),
                pane.session_id,
                app.focused is pane,
                modeline(app)[0],
            )
        )
        # Claude lists the new process with its session id (before any transcript exists)
        live["new-sid"] = interactive("new-sid", pane.pid)
        app.refresh_live()
        await settle(app, pilot)
        seen.append(
            ("listed", pane.session_id, app.shown_session(), modeline(app)[0], table.cursor_row)
        )
        # the first message writes the transcript; the next scan shows its row
        transcript("new-sid", user("my new question"), mtime=9_000)
        app.reload()
        await settle(app, pilot)
        seen.append(
            (
                "row",
                app.rows[0].head.session_id,
                table.cursor_row,
                table.get_row_at(0)[0],
                modeline(app)[0],
            )
        )

    drive(monkeypatch, scenario, live=live)
    assert seen == [
        ("started", "FAKE", "", True, "▶ new conversation"),
        ("listed", "new-sid", "new-sid", "▶ proj-d1", 2),  # no row yet: the cursor stays
        ("row", "new-sid", 0, "▶", "▶ my new question"),  # the cursor goes to its row
    ]


def test_a_pid_from_another_terminal_does_not_tag_the_new_pane(three, monkeypatch) -> None:
    live = {"other": interactive("other", 1)}  # pid 1: not our pane's
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("ctrl+n")
        await settle(app, pilot)
        app.refresh_live()
        await settle(app, pilot)
        seen.append(app._shown_pane().session_id)

    drive(monkeypatch, scenario, live=live)
    assert seen == [""]


def test_n_drops_a_new_conversation_that_ended_without_an_id(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("ctrl+n")
        await settle(app, pilot)
        first = app._shown_pane()
        os.kill(first.pid, 9)
        assert await wait_until(pilot, lambda: first.exited)
        await settle(app, pilot)  # keys back to the list
        await pilot.press("ctrl+n")
        await settle(app, pilot)
        seen.extend([len(app._panes()), app._shown_pane() is not first])

    drive(monkeypatch, scenario)
    assert seen == [1, True]


# --- a: another directory's conversation ------------------------------------------------


@_needs_proc
def test_another_directorys_conversation_runs_in_its_directory(
    three, monkeypatch, tmp_path
) -> None:
    import json

    other = tmp_path / "other dir"  # a space: the path is quoted
    other.mkdir()
    pdir = sessions.project_dir(str(other))
    pdir.mkdir(parents=True)
    path = pdir / "there.jsonl"
    path.write_text(json.dumps({"sessionId": "there", **user("q", cwd=str(other))}) + "\n")
    os.utime(path, (9_000, 9_000))  # newest: first row
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("a", "a")  # all directories
        await settle(app, pilot)
        pane = await open_row(app, pilot, 0)
        assert await wait_until(pilot, lambda: "CWD" in screen(pane))
        # the printed path wraps at the pty's first 80 columns: ask the process instead
        seen.extend([screen(pane).splitlines()[0].rstrip(), os.readlink(f"/proc/{pane.pid}/cwd")])

    drive(monkeypatch, scenario)
    assert seen == ["FAKE --resume there", str(other)]


# --- ← retagging: a title is matched once, against a snapshot newer than it -------------


def test_a_renamed_session_is_not_retagged_to_another_of_that_name(three, monkeypatch) -> None:
    import threading

    live = {"s0": interactive("s0", None, name="alpha"), "s2": background("s2", name="beta")}
    started, release = threading.Event(), threading.Event()
    seen: list = []

    def held_poll():  # its snapshot is taken when the poll starts
        snapshot = dict(live)
        started.set()
        release.wait(5)
        return snapshot

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        monkeypatch.setattr(agents, "live_agents", held_poll)
        app.refresh_live()
        assert await wait_until(pilot, started.is_set)  # a poll from before the rename runs
        monkeypatch.setattr(agents, "live_agents", lambda: dict(live))
        live["s0"] = interactive("s0", None, name="beta")  # /rename: s0 is "beta" now
        pane.post_message(Terminal.TitleChanged("beta", ""))
        await pilot.pause()
        release.set()  # the old snapshot (s0 "alpha") arrives
        await settle(app, pilot)
        seen.append(pane.session_id)

    drive(monkeypatch, scenario, live=live)
    assert seen == ["s0"]  # matched only with the newer snapshot, where s0 is "beta" too


def test_a_matched_title_is_not_matched_again(three, monkeypatch) -> None:
    live = {"s2": background("s2", name="bravo")}
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        pane.post_message(Terminal.TitleChanged("bravo", ""))
        await settle(app, pilot)
        seen.append(pane.session_id)  # ← to s2
        live.clear()
        live["s1"] = background("s1", name="bravo")  # later another agent has the name
        app.refresh_live()
        await settle(app, pilot)
        seen.append(pane.session_id)

    drive(monkeypatch, scenario, live=live)
    assert seen == ["s2", "s2"]


def test_a_title_waits_for_its_session_to_be_listed(three, monkeypatch) -> None:
    live: dict = {}
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        pane.post_message(Terminal.TitleChanged("cont name", ""))  # ← made a continuation
        await settle(app, pilot)
        seen.append(pane.session_id)  # not listed yet
        live["s2"] = background("s2", name="cont name")  # now it is
        app.refresh_live()
        await settle(app, pilot)
        seen.append(pane.session_id)

    drive(monkeypatch, scenario, live=live)
    assert seen == ["s0", "s2"]


# --- open conversations across runs ---------------------------------------------------


def saved() -> OpenState:
    return state.load(PROJECT)


def last_run(**fields) -> None:
    """What a previous run left in the state file."""
    state.save(OpenState(cwd=PROJECT, **fields))


def test_open_conversations_are_saved_as_they_change(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        await open_row(app, pilot, 0)
        await open_row(app, pilot, 1)
        seen.append(saved())  # on disk while running: also after a crash

    drive(monkeypatch, scenario)
    assert seen[0].open == ("s0", "s1") and seen[0].shown == "s1"


def test_the_next_start_opens_the_shown_one_and_marks_the_others(three, monkeypatch) -> None:
    last_run(open=("s0", "s1"), shown="s1")
    seen: list = []

    async def scenario(app, pilot) -> None:
        table = app.query_one(DataTable)
        pane = app._shown_pane()
        assert pane is not None and await wait_until(pilot, lambda: "FAKE" in screen(pane))
        seen.append(
            (
                screen(pane).splitlines()[0].rstrip(),
                isinstance(app.focused, DataTable),
                [table.get_row_at(i)[0] for i in range(3)],
                table.cursor_row,
            )
        )
        await pilot.press(*TOP, "enter")  # s0: started now
        await settle(app, pilot)
        seen.append(([table.get_row_at(i)[0] for i in range(3)], app.was_open, saved().open))

    drive(monkeypatch, scenario)
    assert seen == [
        ("FAKE --resume s1", True, ["○", "▶", ""], 1),  # the keys stay in the list
        (["▶", "●", ""], (), ("s1", "s0")),
    ]


def test_quit_keeps_them_for_the_next_start(three, monkeypatch) -> None:
    async def scenario(app, pilot) -> None:
        await open_row(app, pilot, 0)
        await open_row(app, pilot, 1)
        catch_exit(app)
        await pilot.press("alt+s", "q", "y", "e", "s", "enter")
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert (saved().open, saved().shown) == (("s0", "s1"), "s1")


def test_exit_inside_claude_drops_it(three, monkeypatch) -> None:
    async def scenario(app, pilot) -> None:
        first = await open_row(app, pilot, 0)
        await open_row(app, pilot, 1)
        os.kill(first.pid, 9)  # like /exit
        assert await wait_until(pilot, lambda: first.exited)
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert saved().open == ("s1",)


def test_x_closes_a_running_conversation_after_yes(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        pid = pane.pid
        await pilot.press("alt+s", *TOP, "x", "y", "e", "s", "enter")
        await settle(app, pilot)
        seen.extend(
            [
                agents.wait_exit([pid], timeout=3),
                app._panes(),
                app.shown_session(),
                app.query_one(DataTable).get_row_at(0)[0],
            ]
        )

    drive(monkeypatch, scenario)
    assert seen == [True, [], None, ""]
    assert saved().open == ()


def test_x_forgets_a_conversation_that_was_open_last_time(three, monkeypatch) -> None:
    last_run(open=("s0", "s1"), shown="s1")
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press(*TOP, "x")  # s0: ○, nothing runs: no question
        await settle(app, pilot)
        seen.append(app.query_one(DataTable).get_row_at(0)[0])

    drive(monkeypatch, scenario)
    assert seen == [""] and saved().open == ("s1",)


def test_a_conversation_running_elsewhere_is_not_started_at_restore(three, monkeypatch) -> None:
    last_run(open=("s1",), shown="s1")
    live = {"s1": interactive("s1", 12345)}  # another terminal runs it

    app = drive(monkeypatch, lambda app, pilot: settle(app, pilot), live=live)
    assert app._panes() == [] and app.was_open == ("s1",)


def test_the_scope_starts_on_the_current_folder(three, monkeypatch) -> None:
    last_run(all_dirs=True)
    app = drive(monkeypatch, lambda app, pilot: settle(app, pilot))
    assert app.scope == "here"


def test_a_rotates_the_scope(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        for _ in range(3):
            await pilot.press("a")
            await settle(app, pilot)
            seen.append(app.scope)

    drive(monkeypatch, scenario)
    assert seen == ["subfolders", "all", "here"]


def test_delete_drops_it_from_the_open_ones(three, monkeypatch) -> None:
    last_run(open=("s0", "s1"), shown="s1")

    async def scenario(app, pilot) -> None:
        await pilot.press(*TOP, "ctrl+d", "y", "e", "s", "enter")  # s0 (○)
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert saved().open == ("s1",)


def test_a_new_conversation_is_saved_once_it_has_an_id(three, monkeypatch) -> None:
    live: dict = {}
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("ctrl+n")
        await settle(app, pilot)
        seen.append(saved().open)  # no id yet: nothing to reopen
        live["new-sid"] = interactive("new-sid", app._shown_pane().pid)
        app.refresh_live()
        await settle(app, pilot)
        seen.append(saved().open)

    drive(monkeypatch, scenario, live=live)
    assert seen == [(), ("new-sid",)]


def test_the_file_keeps_the_last_run_until_it_is_restored(three, monkeypatch) -> None:
    import threading

    last_run(open=("s0", "s1"), shown="s1")
    release = threading.Event()
    real_rows, real_polled = sessions.build_rows, SwitcherApp._polled
    seen: list = []

    def held(cwd, cancelled, only, subfolders=False):
        release.wait(5)  # the first scan comes after the first poll
        return real_rows(cwd, cancelled, only, subfolders)

    def polled(app, *args):
        real_polled(app, *args)
        if not release.is_set():  # the first poll is in, the scan is not
            seen.append((app._restored, saved().open))  # a crash now must lose nothing
            release.set()

    monkeypatch.setattr(sessions, "build_rows", held)
    monkeypatch.setattr(SwitcherApp, "_polled", polled)

    async def scenario(app, pilot) -> None:
        seen.append((app._restored, saved().open))

    drive(monkeypatch, scenario)
    assert seen == [(False, ("s0", "s1")), (True, ("s1", "s0"))]


def test_main_moves_the_old_files_and_says_so(monkeypatch, tmp_path, claude_home) -> None:
    old = claude_home / "session-switcher-meta.json"  # where an early version kept it
    old.write_text('{"s1": {"label": "x"}}')
    notes: list = []
    monkeypatch.setattr(app_module.SwitcherApp, "run", lambda self, **_k: notes.extend(self._notes))
    monkeypatch.setattr(app_module.sys, "argv", ["app.py", str(tmp_path)])
    assert app_module.main() == 0
    assert notes == [f"Moved labels, groups and tags -> {sessions.sidecar_path()}"]
    assert not old.exists()


def test_start_notes_are_shown(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        seen.extend(n.message for n in app._notifications)

    import pilot as pilot_module  # drive() builds the app there

    monkeypatch.setattr(
        pilot_module, "SwitcherApp", lambda cwd: SwitcherApp(cwd, notes=("Moved x",))
    )
    drive(monkeypatch, scenario)
    assert seen == ["Moved x"]


# --- CPU, wakeups and memory of the sessions ------------------------------------------


async def two_samples(app, pilot) -> None:
    for _ in range(2):
        app._sample_tick(force=True)
        await settle(app, pilot)
        await pilot.pause(0.3)


@_needs_proc
def test_a_running_conversation_shows_its_cpu_and_memory(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        await two_samples(app, pilot)
        table = app.query_one(DataTable)
        cpu, mem = str(table.get_cell_at(Coordinate(0, 3))).split("\n")
        seen.extend(
            [
                pane.pid in app.uses,
                cpu.strip(),
                mem.strip().endswith("M"),
                str(table.get_cell_at(Coordinate(1, 3))),
                groups(app)[-1][2:],
                groups(app)[-1][:2],
            ]
        )

    drive(monkeypatch, scenario)
    in_uses, cpu, mem_ok, idle_row, line, this = seen
    assert in_uses and cpu in ("0%", "<1%") and mem_ok  # the fake Claude is an idle cat
    assert idle_row == ""  # nothing runs for that row
    # one conversation runs, so "all running" would repeat "this one": left out
    assert [x.split()[0] for x in line if x] == ["workspace"]
    line = [x for x in line if x]
    header, this = this
    assert header.split() == ["cpu", "mem"] and this.startswith("this one ")
    assert this.split()[2:] != ["-", "-"], this
    # every row is right-aligned to the header, so the digits line up
    assert len({len(x) for x in [header, this, *line]}) == 1


@_needs_proc
def test_t_shows_each_session_and_its_processes(three, monkeypatch) -> None:
    from cabildo.workspace.app import TopScreen

    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        await pilot.press("alt+s", "T")
        assert await wait_until(pilot, lambda: bool(app.uses), timeout=5)  # ~0.5 s
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TopScreen)
        table = screen.query_one(DataTable)
        labels = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        seen.extend([labels, pane.pid])
        await pilot.press("q")
        await pilot.pause()
        seen.append(isinstance(app.screen, TopScreen))

    drive(monkeypatch, scenario)
    labels, pid, still_open = seen
    assert labels[0] == "▶ session s0"  # the shown conversation
    assert labels[1].strip() == f"cat {pid}"  # the fake Claude's process
    assert labels[-2:] == ["workspace (this app)", "Total"]
    assert not still_open


def test_nothing_is_sampled_while_the_list_is_hidden(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        await open_row(app, pilot, 0)
        await pilot.press("alt+z")  # hide the list
        before = app._sampled_at
        app._sample_tick(force=True)
        await settle(app, pilot)
        seen.append(app._sampled_at == before)

    drive(monkeypatch, scenario)
    assert seen == [True]


def test_what_is_sampled(three, monkeypatch) -> None:
    def working(sid: str, pid: int) -> agents.AgentState:
        return agents.AgentState(
            session_id=sid,
            agent_id=sid,
            kind="background",
            name="n",
            cwd="/x",
            status="",
            state="working",
            pid=pid,
        )

    live = {"s2": working("s2", 424242), "elsewhere": working("elsewhere", 535353)}
    seen: list = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        seen.append(
            (app._usage_roots(everything=False), app._usage_roots(everything=True), pane.pid)
        )

    drive(monkeypatch, scenario, live=live)
    in_list, everything, pid = seen[0]
    me = os.getpid()
    assert in_list == {pid: True, 424242: True, me: False}  # the list's; the app alone
    assert everything == {pid: True, 424242: True, 535353: True, me: False}  # the top view


def tokens(app: SwitcherApp) -> list[list[str]]:
    """The mode line's model and tokens group; none before the first reply."""
    return [g for g in groups(app) if g[-1].startswith("tokens")]


def test_the_mode_line_shows_the_shown_conversations_tokens(
    transcript, fake_claude, monkeypatch
) -> None:
    transcript("s0", user("hi"), reply("msg_a", (2, 53_000, 0), 1500), mtime=3_000)
    transcript("s1", user("no reply yet"), mtime=2_000)
    seen: list = []

    async def scenario(app, pilot) -> None:
        seen.append(tokens(app))  # nothing shown
        await open_row(app, pilot, 0)
        seen.append(tokens(app))
        await open_row(app, pilot, 1)
        seen.append(tokens(app))

    drive(monkeypatch, scenario)
    assert seen == [
        [],
        [["opus 5.5      ctx 53k     of 1M   5%", "tokens         in 53k    out 2k"]],
        [],
    ]


def test_claude_starts_with_the_launch_of_the_conversations_group(three, monkeypatch) -> None:
    sessions.update_meta("s2", lambda m: sessions.Meta(group="Work"))  # first row: grouped
    config = Config(launch=Launch(model="m-default"), groups={"work": Launch(model="m-work")})
    seen: list = []

    async def scenario(app, pilot) -> None:
        for row in (0, 1):
            pane = await open_row(app, pilot, row)
            seen.append(screen(pane).splitlines()[0].rstrip())
        await pilot.press("alt+s", "ctrl+n")
        await settle(app, pilot)
        pane = app._shown_pane()
        assert pane is not None
        assert await wait_until(pilot, lambda: "FAKE" in screen(pane))
        seen.append(screen(pane).splitlines()[0].rstrip())

    drive(monkeypatch, scenario, config=config)
    assert seen == [
        "FAKE --resume s2 --model m-work",
        "FAKE --resume s0 --model m-default",
        "FAKE --model m-default",
    ]


def test_n_under_a_one_group_filter_uses_its_launch_and_joins_it(
    three, transcript, monkeypatch
) -> None:
    sessions.update_meta("s0", lambda m: sessions.Meta(group="work"))
    config = Config(launch=Launch(model="m-default"), groups={"work": Launch(model="m-work")})
    live: dict = {}
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("ctrl+n")
        await settle(app, pilot)
        pane = app._shown_pane()
        assert pane is not None
        assert await wait_until(pilot, lambda: "FAKE" in screen(pane))
        seen.append(screen(pane).splitlines()[0].rstrip())
        live["new-sid"] = interactive("new-sid", pane.pid)
        app.refresh_live()
        await settle(app, pilot)
        transcript("new-sid", user("my new question"), mtime=9_000)
        app.reload()
        await settle(app, pilot)
        seen.append(sessions.load_meta()["new-sid"].group)
        seen.append([r.head.session_id for r in app.rows])  # still in the filtered list

    drive(monkeypatch, scenario, live=live, config=config, only=sessions.Filter(groups=("Work",)))
    assert seen == ["FAKE --model m-work", "Work", ["new-sid", "s0"]]


@_needs_proc
def test_a_conversation_sampled_once_shows_its_memory_and_waits_for_cpu(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        await open_row(app, pilot, 0)
        app._sample_tick(force=True)
        await settle(app, pilot)
        await pilot.pause(0.3)
        seen.append(groups(app)[-1][1])

    drive(monkeypatch, scenario)
    _, _, cpu, mem = seen[0].split()
    assert cpu == "…" and mem.endswith("M")


@_needs_proc
def test_all_running_shows_when_more_than_the_shown_conversation_runs(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        await open_row(app, pilot, 0)
        await open_row(app, pilot, 1)
        await two_samples(app, pilot)
        seen.extend(x.split()[0] for x in groups(app)[-1][1:] if x)

    drive(monkeypatch, scenario)
    assert seen == ["this", "all", "workspace"]


def test_a_hidden_pane_does_not_paint(three, monkeypatch) -> None:
    from collections import Counter

    from textual_tty import Terminal

    ticks: Counter = Counter()
    real = Terminal._tick

    def counting(self) -> None:
        ticks[self.session_id] += 1
        real(self)

    monkeypatch.setattr(Terminal, "_tick", counting)
    seen: list = []

    async def scenario(app, pilot) -> None:
        hidden = await open_row(app, pilot, 0)
        shown = await open_row(app, pilot, 1)
        ticks.clear()
        await pilot.pause(0.5)
        seen.extend([ticks[hidden.session_id], ticks[shown.session_id]])

    drive(monkeypatch, scenario)
    assert seen[0] == 0 and seen[1] > 0  # (#9)


def test_an_unchanged_mode_line_is_not_repainted(three, monkeypatch) -> None:
    seen: list = []

    async def scenario(app, pilot) -> None:
        await settle(app, pilot)
        modeline = app.query_one("#modeline")
        real = modeline.update
        calls: list = []
        monkeypatch.setattr(modeline, "update", lambda *a, **kw: calls.append(kw) or real(*a, **kw))
        app._modeline()
        app._modeline()
        seen.append(calls)

    drive(monkeypatch, scenario)
    assert seen == [[]]  # (#9)


# --- reload: the Claudes are handed over, not closed -------------------------------


def test_reload_hands_its_claudes_over_instead_of_closing_them(three, monkeypatch) -> None:
    """`R` re-execs this process, which keeps its children. So a reload must detach each
    pane -- leaving the child running on a descriptor that survives exec -- rather than
    close it, which is what used to end every session on reload.
    """
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        pid = pane.pid
        assert pid is not None
        app.action_reload()
        await settle(app, pilot)
        seen.update(pid=pid, handoff=os.environ.get(attach.ENV, ""), asked=app.reload_asked)

    drive(monkeypatch, scenario)
    handed = attach.decode(seen["handoff"])
    os.environ.pop(attach.ENV, None)
    try:
        assert seen["asked"] is True
        assert [h.pid for h in handed] == [seen["pid"]]
        assert alive(seen["pid"])  # not hung up: the reload kept it
        assert not fcntl.fcntl(handed[0].fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        for h in handed:
            attach.discard(h.fd)
            attach.hangup(h.pid)


@_needs_proc
def test_a_plain_quit_still_closes_its_claudes(three, monkeypatch) -> None:
    """The detach is for reload alone: quitting must end the sessions as it always did,
    and leave no descriptor written down for a next start to attach to.
    """
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        seen["pid"] = pane.pid
        app._quit()
        await settle(app, pilot)
        seen["handoff"] = os.environ.get(attach.ENV, "")

    drive(monkeypatch, scenario)
    assert seen["handoff"] == ""
    assert agents.wait_exit([seen["pid"]], timeout=3)


def test_a_restarted_app_attaches_to_the_claude_it_was_handed(three, monkeypatch) -> None:
    """The other half of a reload: with a handoff in the environment, the app builds its
    pane around the pseudo-terminal that came through exec instead of spawning a Claude.
    The child here is a plain `cat`, which echoes, so the pane proves it is really wired
    to the process that was already running.
    """
    import pty as _pty
    import subprocess

    master, slave = _pty.openpty()
    child = subprocess.Popen(
        ["cat"], stdin=slave, stdout=slave, stderr=slave, start_new_session=True
    )
    os.close(slave)
    kept = attach.keep_across_exec(master)
    os.close(master)
    monkeypatch.setenv(
        attach.ENV,
        attach.encode(
            [
                attach.Handed(
                    fd=kept,
                    pid=child.pid,
                    pane_id="pane-1",
                    session_id="s0",
                    cwd="",
                    group="",
                    title="alpha",
                    rows=24,
                    cols=80,
                )
            ]
        ),
    )
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        pane = app._shown_pane()
        assert pane is not None
        pane.board.host.write("echoed by the inherited child\n")
        await wait_until(pilot, lambda: "echoed by the inherited child" in screen(pane))
        seen.update(pid=pane.pid, text=screen(pane), spawned=child.poll())

    try:
        drive(monkeypatch, scenario)
        assert seen["pid"] == child.pid  # the pane owns the child it was handed
        assert "echoed by the inherited child" in seen["text"]
        assert seen["spawned"] is None  # and never started a second one
    finally:
        child.kill()
        child.wait()


# --- OSC 9: a notification, or a progress bar wearing its clothes -------------------


def test_a_progress_report_is_not_shown_as_a_notification(three, monkeypatch) -> None:
    """`ESC ] 9 ; 4 ; state ; percent` is ConEmu's taskbar progress, which Claude emits
    while it works. OSC 9 is also iTerm's notification sequence, so a parser that routes
    all of OSC 9 to "notify" pops a toast reading `4;3;` on every turn.
    """
    seen: list[str] = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        monkeypatch.setattr(type(app), "notify", lambda self, text, **kw: seen.append(text))
        pane._notify("4;3;")  # a turn starts
        pane._notify("4;0;")  # and ends
        pane._notify("Build finished")  # a real one
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert seen == ["Build finished"]


def test_the_pane_filters_what_the_terminal_reports(three, monkeypatch) -> None:
    """The filter is wired to the seam the terminal engine actually calls."""
    seen: list[str] = []

    async def scenario(app, pilot) -> None:
        pane = await open_row(app, pilot, 0)
        monkeypatch.setattr(type(app), "notify", lambda self, text, **kw: seen.append(text))
        pane._chrome.on_notify("4;3;")
        pane._chrome.on_notify("done")
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert seen == ["done"]
