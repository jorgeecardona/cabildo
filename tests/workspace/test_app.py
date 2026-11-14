"""app.py: path display, how a row is opened, and the list UI (Textual pilot)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from lines import PROJECT, ai_title, assistant, continued_in, user
from pilot import background, drive, settle, wait_until
from textual.widgets import DataTable, Input, Static

from cabildo.workspace import agents, sessions
from cabildo.workspace.agents import AgentState
from cabildo.workspace.app import CHROME, MIN_WIDTH, LiveState, SwitcherApp, short_path


def test_short_path_uses_tilde_and_keeps_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/u")
    assert short_path("/home/u/stuff/x", 40) == "~/stuff/x"
    assert short_path("/home/u", 40) == "~"
    assert short_path("/home/u2/x", 40) == "/home/u2/x"  # not under home
    cut = short_path("/opt/" + "a" * 50 + "/end", 20)
    assert len(cut) == 20 and cut.startswith("…") and cut.endswith("/end")


# --- how Enter opens a row ---------------------------------------------------


@pytest.fixture
def chain_row(transcript) -> sessions.Row:
    """Origin (real messages) -> continuation stub, as ← in Claude leaves them."""
    transcript("origin-session", user("q"), assistant("a"), continued_in("cont-session"))
    transcript("cont-session", ai_title("continued"))
    (row,) = sessions.build_rows(PROJECT)
    return row


def target(row: sessions.Row, live: LiveState):
    app = SwitcherApp(PROJECT)
    app.live = live
    return app._target(row)


def test_background_head_is_attached(chain_row: sessions.Row) -> None:
    t = target(
        chain_row, LiveState(agents={"cont-session": background("cont-session")}, loaded=True)
    )
    assert t.command == ("claude", "attach", "cont-ses")
    assert (t.session_id, t.session_ids) == ("cont-session", ("origin-session", "cont-session"))


def test_unknown_head_resumes_the_newest_session_with_messages(chain_row: sessions.Row) -> None:
    t = target(chain_row, LiveState(agents={}, loaded=True))  # e.g. removed with `claude rm`
    assert (t.command, t.session_id) == (("claude", "--resume", "origin-session"), "origin-session")


def test_before_the_first_poll_a_continuation_is_attached(chain_row: sessions.Row) -> None:
    t = target(chain_row, LiveState())  # agents not loaded yet
    assert t.command == ("claude", "attach", "cont-ses")


def test_plain_session_is_resumed(transcript) -> None:
    transcript("plain-session", user("q"), assistant("a"))
    (row,) = sessions.build_rows(PROJECT)
    assert target(row, LiveState(loaded=True)).command == ("claude", "--resume", "plain-session")


# --- the list ------------------------------------------------------------------


def first_row(app: SwitcherApp) -> list:
    return app.query_one(DataTable).get_row_at(0)


def test_row_shows_title_then_cwd_and_branch(transcript, monkeypatch) -> None:
    transcript(
        "s1",
        user("Fix the login bug", branch="feat/login"),
        {"type": "attachment", "cwd": f"{PROJECT}/web"},
    )
    cells: list = []

    async def scenario(app, pilot) -> None:
        cells.extend(first_row(app))

    drive(monkeypatch, scenario)
    assert str(cells[2]) == f"Fix the login bug\n{PROJECT}/web · feat/login"


def test_list_width_is_the_content_width(transcript, monkeypatch) -> None:
    title = "A fairly long title for one of the sessions"
    for i in range(30):  # enough rows for a scrollbar
        transcript(f"s{i:02}", user(f"{title} {i:02}"))
    seen: list = []

    async def scenario(app, pilot) -> None:
        table = app.query_one(DataTable)
        seen.extend(
            [
                app.list_width,
                app.query_one("#side").outer_size.width,
                table.virtual_size.width,
                table.scrollable_content_region.width,
            ]
        )

    drive(monkeypatch, scenario, size=(200, 40))
    list_width, side, content, room = seen
    assert list_width == len(title) + 3 + 3 + CHROME  # longest line + "Age" + chrome
    assert side == list_width  # the list pane got that width
    assert content == room  # fits exactly: no cut, no gap


def test_short_content_gets_the_minimum_width(transcript, monkeypatch) -> None:
    transcript("s1", user("hi", branch="HEAD"))  # lines "hi" and "/work/proj"
    app = drive(monkeypatch, lambda app, pilot: settle(app, pilot), size=(200, 40))
    assert app.list_width == MIN_WIDTH


def test_text_is_cut_to_half_the_window(transcript, monkeypatch) -> None:
    transcript("s1", user("x" * 100))
    cells: list = []

    async def scenario(app, pilot) -> None:
        cells.extend(first_row(app))

    app = drive(monkeypatch, scenario, size=(100, 30))
    assert app.list_width == 50
    assert len(str(cells[2]).split("\n")[0]) == 50 - CHROME - 3


def test_minibuffer_keys_are_text_not_commands(transcript, monkeypatch) -> None:
    transcript("s1", user("q"))

    async def scenario(app, pilot) -> None:
        for key in ["t", "q", "l", "j", "enter"]:  # q/l/j would quit/label/move
            await pilot.press(key)
        await settle(app, pilot)
        assert app.is_running

    drive(monkeypatch, scenario)
    assert sessions.load_meta()["s1"].tags == ("qlj",)


def test_group_prompt_lists_existing_groups(transcript, monkeypatch) -> None:
    transcript("s1", user("q"))
    # groups from other directories too: the sidecar is global
    sessions.save_meta(
        {
            "a": sessions.Meta(group="Work"),
            "b": sessions.Meta(group="infra"),
            "c": sessions.Meta(group="network"),
        }
    )
    shown: list[str] = []

    async def scenario(app, pilot) -> None:
        def choices() -> str:
            return str(app.screen.query_one("#mb-choices", Static).render())

        await pilot.press("g")
        await pilot.pause()
        shown.append(choices())  # before typing: every group
        await pilot.press("w")
        await pilot.pause()
        shown.append(choices())  # prefix match first, then the other match
        await pilot.press("x")
        await pilot.pause()
        shown.append(choices())
        for key in ["backspace", "tab", "enter"]:
            await pilot.press(key)
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert shown == ["infra   network   Work", "Work   network", "(no match: a new one)"]
    assert sessions.load_meta()["s1"].group == "Work"


# --- filter ------------------------------------------------------------------------


def grouped(transcript) -> None:
    for i, sid in enumerate(["s0", "s1", "s2"]):
        transcript(sid, user(f"session {sid}"), mtime=3_000 - i)
    sessions.save_meta(
        {
            "s0": sessions.Meta(group="work", tags=("urgent",)),
            "s1": sessions.Meta(group="work"),
            "s2": sessions.Meta(group="home", tags=("urgent",)),
        }
    )


def listed(app: SwitcherApp) -> list[str]:
    return [r.head.session_id for r in app.rows]


def test_f_filters_the_list_and_the_filter_is_saved(transcript, monkeypatch) -> None:
    grouped(transcript)
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("f")
        await pilot.pause()
        seen.append(str(app.screen.query_one("#mb-choices", Static).render()))
        for key in ["g", "colon", "w", "tab", "t", "colon", "u", "tab", "enter"]:
            await pilot.press(key)
        await settle(app, pilot)
        seen.append(listed(app))
        seen.append(str(app.query_one("#modeline").render()).split("\n")[0])
        for key in ["f", "ctrl+u", "enter"]:  # empty: every conversation again
            await pilot.press(key)
        await settle(app, pilot)
        seen.append(listed(app))

    drive(monkeypatch, scenario)
    assert seen == [
        "g:home   g:work   t:urgent",
        ["s0"],
        "proj g:work t:urgent  ·  1 session",
        ["s2", "s0", "s1"],  # home first, then work newest-first
    ]


@pytest.mark.parametrize(
    ("typed", "after_tab"),
    [
        ("g:wo", "g:work "),
        ("g:work ", "g:work "),  # after a space Tab does nothing
        ("g:work t:ur", "g:work t:urgent "),
        ("t:urgent g:", "t:urgent g:home "),
        ("'g:my", "'g:my team' "),  # a quoted name with a space is one word
        ("g:work 'g:my te", "g:work 'g:my team' "),
    ],
)
def test_tab_in_the_filter_completes_the_last_word(
    transcript, monkeypatch, typed: str, after_tab: str
) -> None:
    grouped(transcript)
    sessions.update_meta("s1", lambda m: replace(m, group="my team"))
    seen: list[str] = []

    async def scenario(app, pilot) -> None:
        await pilot.press("f")
        field = app.screen.query_one(Input)
        field.value = typed
        await pilot.press("tab")
        seen.append(field.value)

    drive(monkeypatch, scenario)
    assert seen == [after_tab]


def test_a_bad_filter_changes_nothing(transcript, monkeypatch) -> None:
    grouped(transcript)
    seen: list = []

    async def scenario(app, pilot) -> None:
        for key in ["f", "w", "enter"]:
            await pilot.press(key)
        await settle(app, pilot)
        seen.extend([len(app.rows), notes(app)])

    drive(monkeypatch, scenario)
    assert seen == [3, ["Filter not changed: not g:<group> or t:<tag>: w"]]


def test_the_filter_comes_back_and_the_command_line_replaces_it(transcript, monkeypatch) -> None:
    from cabildo.workspace import state
    from cabildo.workspace.sessions import Filter

    grouped(transcript)
    state.save(state.OpenState(cwd=PROJECT, filter=Filter(groups=("work",))))
    app = drive(monkeypatch, lambda app, pilot: settle(app, pilot))
    assert sorted(listed(app)) == ["s0", "s1"]
    app = drive(monkeypatch, lambda app, pilot: settle(app, pilot), only=Filter(tags=("urgent",)))
    assert sorted(listed(app)) == ["s0", "s2"]
    assert state.load(PROJECT).filter == Filter(tags=("urgent",))


def test_command_line_flags() -> None:
    from cabildo.workspace.app import parse_args

    args = parse_args(["~/x", "-a", "-G", "work", "--group", "home", "-t", "urgent"])
    assert (args.working_dir, args.all, args.group, args.tag) == (
        "~/x",
        True,
        ["work", "home"],
        ["urgent"],
    )
    args = parse_args([])
    assert (args.working_dir, args.all, args.group, args.tag) == (None, False, [], [])
    assert parse_args(["-G", " work "]).group == ["work"]


@pytest.mark.parametrize("flags", [["-G", ""], ["-t", "  "]])
def test_an_empty_group_or_tag_is_refused(flags: list[str]) -> None:
    from cabildo.workspace.app import parse_args

    with pytest.raises(SystemExit):
        parse_args(flags)


# --- delete ------------------------------------------------------------------------


async def say_yes_to_delete(app, pilot) -> None:
    for key in ["ctrl+d", "y", "e", "s", "enter"]:
        await pilot.press(key)
    await settle(app, pilot)


def notes(app: SwitcherApp) -> list[str]:
    return [n.message for n in app._notifications]


def test_c_d_asks_to_delete_and_needs_yes(transcript, monkeypatch) -> None:
    from cabildo.workspace.app import EditScreen

    path = transcript("s1", user("q"), assistant("a"))
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("ctrl+d")
        await pilot.pause()
        prompt = (
            app.screen.query_one("#mb-prompt").render()
            if isinstance(app.screen, EditScreen)
            else None
        )
        seen.append(str(prompt))
        for k in ["n", "o", "enter"]:
            await pilot.press(k)
        await settle(app, pilot)
        seen.extend([app.is_running, notes(app)])

    drive(monkeypatch, scenario)
    assert seen == ['Delete "q"? (yes or no) ', True, []]
    assert path.exists()


def test_delete_trashes_the_whole_conversation(transcript, monkeypatch) -> None:
    origin = transcript("origin-session", user("q"), assistant("a"), continued_in("cont-session"))
    cont = transcript("cont-session", ai_title("continued"))
    sessions.save_meta({"origin-session": sessions.Meta(tags=("x",))})
    removed: list[str] = []
    monkeypatch.setattr(agents, "remove", lambda agent_id: removed.append(agent_id))
    app = drive(monkeypatch, say_yes_to_delete, live={"cont-session": background("cont-session")})
    assert removed == ["cont-ses"]  # background session stopped + rm'd
    assert not origin.exists() and not cont.exists()
    (folder,) = sessions.trash_dir().iterdir()
    assert sorted(p.name for p in folder.iterdir()) == [
        "RESTORE.txt",
        "cont-session.jsonl",
        "origin-session.jsonl",
    ]
    assert sessions.load_meta() == {}
    assert app.rows == []


def test_delete_refuses_a_session_running_in_another_terminal(transcript, monkeypatch) -> None:
    path = transcript("s1", user("q"))
    elsewhere = AgentState(
        session_id="s1",
        agent_id="s1",
        kind="interactive",
        name="n",
        cwd="/x",
        status="idle",
        state="",
        pid=12345,
    )
    app = drive(monkeypatch, say_yes_to_delete, live={"s1": elsewhere})
    assert path.exists()
    assert notes(app) == ["Running in another terminal: exit it there first"]


def test_delete_stops_when_claude_refuses(transcript, monkeypatch) -> None:
    path = transcript("s1", user("q"))
    monkeypatch.setattr(agents, "remove", lambda _id: "worktree has unpushed commits")
    app = drive(monkeypatch, say_yes_to_delete, live={"s1": background("s1")})
    assert path.exists()
    assert notes(app) == ["Not deleted: worktree has unpushed commits"]


def test_delete_stops_when_claude_agents_fails(transcript, monkeypatch) -> None:
    path = transcript("s1", user("q"))

    async def scenario(app, pilot) -> None:
        monkeypatch.setattr(agents, "live_agents", lambda: None)  # e.g. timed out
        await say_yes_to_delete(app, pilot)

    app = drive(monkeypatch, scenario)
    assert path.exists()
    assert notes(app) == [
        ("Not deleted: `claude agents` failed, so where the conversation runs is not known")
    ]


def test_a_second_delete_waits_for_the_first(transcript, monkeypatch) -> None:
    import threading

    from cabildo.workspace.app import EditScreen

    transcript("s1", user("q"))
    release = threading.Event()

    def slow():
        release.wait(5)
        return {}

    seen: list = []

    async def scenario(app, pilot) -> None:
        monkeypatch.setattr(agents, "live_agents", slow)
        await pilot.press("ctrl+d", "y", "e", "s", "enter")  # the first delete runs
        await pilot.pause()
        await pilot.press("ctrl+d")  # again, same row
        await pilot.pause()
        seen.extend([isinstance(app.screen, EditScreen), notes(app)])
        release.set()
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert seen == [False, ["A delete is still running"]]
    assert len(list(sessions.trash_dir().iterdir())) == 1


@pytest.mark.parametrize(
    ("start", "after"),
    [
        (2, ("s1", 1)),  # last row deleted -> the one above
        (1, ("s0", 0)),  # middle row -> the one above
        (0, ("s1", 0)),  # first row -> the one below, now first
    ],
)
def test_after_delete_the_cursor_goes_to_the_row_above(
    transcript, monkeypatch, start: int, after: tuple[str, int]
) -> None:
    for i, sid in enumerate(["s0", "s1", "s2"]):
        transcript(sid, user(f"session {sid}"), mtime=3_000 - i)
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press(*["j"] * start)
        await say_yes_to_delete(app, pilot)
        table = app.query_one(DataTable)
        seen.extend([app.rows[table.cursor_row].head.session_id, table.cursor_row])

    drive(monkeypatch, scenario)
    assert tuple(seen) == after


def test_list_stays_live_without_moving_the_cursor(transcript, monkeypatch) -> None:
    import os

    for i, sid in enumerate(["s0", "s1", "s2"]):
        transcript(sid, user(f"session {sid}"), mtime=3_000 - i)
    seen: list = []

    async def scenario(app, pilot) -> None:
        table = app.query_one(DataTable)
        await pilot.press("j", "j")  # cursor on s2
        app.reload()  # nothing changed
        await settle(app, pilot)
        seen.append([r.head.session_id for r in app.rows][table.cursor_row])
        path = sessions.project_dir(PROJECT) / "s2.jsonl"
        os.utime(path, (9_000, 9_000))  # s2 used again: now newest
        app.reload()
        await settle(app, pilot)
        seen.append([r.head.session_id for r in app.rows])
        seen.append(app.rows[table.cursor_row].head.session_id)

    drive(monkeypatch, scenario)
    assert seen == ["s2", ["s2", "s0", "s1"], "s2"]  # order updates, cursor follows s2


# --- a: this directory / all directories ----------------------------------------------


def write_in(folder_of: str, sid: str, cwd: str, mtime: float) -> None:
    """A transcript in the folder Claude keeps for `folder_of`."""
    import json
    import os

    pdir = sessions.project_dir(folder_of)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{sid}.jsonl"
    path.write_text(json.dumps({"sessionId": sid, **user(f"prompt {sid}", cwd=cwd)}) + "\n")
    os.utime(path, (mtime, mtime))


def test_a_toggles_this_directory_and_all_directories(transcript, monkeypatch, tmp_path) -> None:
    transcript("here", user("prompt here"), mtime=2_000)
    other = tmp_path / "other"
    other.mkdir()
    write_in(str(other), "there", str(other), mtime=3_000)
    import threading

    release = threading.Event()
    real = sessions.build_rows

    def held(cwd, cancelled, only, subfolders=False):  # every directory: held until released
        if cwd is None:
            release.wait(5)
        return real(cwd, cancelled, only, subfolders)

    monkeypatch.setattr(sessions, "build_rows", held)
    seen: list = []

    def modeline(app) -> list[str]:
        return str(app.query_one("#modeline").render()).split("\n")

    def look(app) -> tuple:
        table = app.query_one(DataTable)
        header = str(list(table.columns.values())[2].label)
        return [r.head.session_id for r in app.rows], header, modeline(app)[0]

    async def scenario(app, pilot) -> None:
        seen.append(look(app))
        await pilot.press("a", "a")  # here, subfolders, all
        await pilot.pause()
        seen.append(modeline(app)[1])  # the scan is on its way
        release.set()
        await settle(app, pilot)
        seen.append(look(app))
        await pilot.press("a")
        await settle(app, pilot)
        seen.append(look(app))

    drive(monkeypatch, scenario)
    assert seen == [
        (["here"], "Session", "proj  ·  1 session"),
        "reading transcripts…",
        (["there", "here"], "Session · all", "all directories  ·  2 sessions"),
        (["here"], "Session", "proj  ·  1 session"),
    ]


def test_another_directorys_conversation_starts_in_its_directory(transcript, tmp_path) -> None:
    transcript("here", user("q"), assistant("a"))
    other = tmp_path / "other"
    other.mkdir()
    write_in(str(other), "there", str(other), mtime=3_000)
    rows = {r.head.session_id: r for r in sessions.build_rows(None)}
    assert target(rows["here"], LiveState(loaded=True)).cwd is None  # where the app runs
    t = target(rows["there"], LiveState(loaded=True))
    assert (t.command, t.cwd) == (("claude", "--resume", "there"), str(other))


def test_a_conversation_whose_directory_is_gone_is_not_opened(
    claude_home, monkeypatch, tmp_path
) -> None:
    gone = tmp_path / "gone"
    write_in(str(gone), "lost", str(gone), mtime=3_000)
    seen: list = []

    async def scenario(app, pilot) -> None:
        await pilot.press("a", "a")  # all directories
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        seen.extend(
            [
                [r.head.session_id for r in app.rows],
                app._panes(),
                [n.message for n in app._notifications],
            ]
        )

    drive(monkeypatch, scenario)
    rows, panes, notes = seen
    assert rows == ["lost"] and panes == []
    assert len(notes) == 1 and notes[0].startswith("Not opened: its directory")


def test_a_conversation_whose_directory_is_gone_has_no_target(claude_home, tmp_path) -> None:
    gone = tmp_path / "gone"
    write_in(str(gone), "lost", str(gone), mtime=3_000)
    (row,) = sessions.build_rows(None)
    assert target(row, LiveState(loaded=True)) is None
    attach = target(row, LiveState(agents={"lost": background("lost")}, loaded=True))
    assert attach is not None and attach.command == ("claude", "attach", "lost")  # still runs


# --- polls: one at a time, in order; a failed one keeps the last snapshot -------------


def test_polls_run_one_at_a_time_and_once_more_after(transcript, monkeypatch) -> None:
    import threading

    transcript("s1", user("q"))
    release = threading.Event()
    state = {"running": 0, "most": 0, "calls": 0}
    lock = threading.Lock()

    def slow():
        with lock:
            state["calls"] += 1
            state["running"] += 1
            state["most"] = max(state["most"], state["running"])
        release.wait(5)
        with lock:
            state["running"] -= 1
        return {}

    async def scenario(app, pilot) -> None:
        monkeypatch.setattr(agents, "live_agents", slow)
        for _ in range(5):
            app.refresh_live()  # e.g. a new pane, titles, the 10 s tick
        await pilot.pause(0.2)
        release.set()
        await settle(app, pilot)

    drive(monkeypatch, scenario)
    assert (state["calls"], state["most"]) == (2, 1)  # the running poll, then one more


def test_a_failed_poll_keeps_the_last_snapshot(transcript, monkeypatch) -> None:
    transcript("s1", user("q"))
    seen: list = []

    async def scenario(app, pilot) -> None:
        seen.append(set(app.live.agents))
        monkeypatch.setattr(agents, "live_agents", lambda: None)
        app.refresh_live()
        await settle(app, pilot)
        seen.append(set(app.live.agents))

    drive(monkeypatch, scenario, live={"s1": background("s1")})
    assert seen == [{"s1"}, {"s1"}]


def test_a_scan_that_ends_after_quit_changes_nothing(transcript, monkeypatch) -> None:
    import threading

    transcript("s1", user("q"))
    entered, release = threading.Event(), threading.Event()
    real = sessions.build_rows
    late: list = []

    def slow(cwd, _cancelled, _only, _subfolders=False):
        entered.set()
        release.wait(5)
        return real(cwd)  # ignores the cancel: only the UI guard is left

    async def scenario(app, pilot) -> None:
        monkeypatch.setattr(sessions, "build_rows", slow)
        app.reload()
        assert await wait_until(pilot, entered.is_set)
        app._apply_rows = lambda *a: late.append(a)  # type: ignore[method-assign]
        threading.Timer(0.5, release.set).start()  # after the app has closed

    drive(monkeypatch, scenario)
    assert late == []


def test_the_context_line_has_no_model_part_when_the_model_is_unknown() -> None:
    from cabildo.workspace.app import fmt_context, grid
    from cabildo.workspace.sessions import Tokens

    assert fmt_context(Tokens(53_000, 53_000, 2000, model="")) == grid("context", "ctx 53k", "")
    assert fmt_context(Tokens(53_000, 53_000, 2000, model="claude-opus-5-5")) == (
        "opus 5.5      ctx 53k     of 1M   5%"
    )


def test_the_tokens_line_shows_cache_reads_apart_from_input() -> None:
    from cabildo.workspace.app import fmt_tokens
    from cabildo.workspace.sessions import Tokens

    t = Tokens(224_000, 31_200_000, 139_000, cached=30_000_000)
    assert fmt_tokens(t).split() == ["tokens", "in", "1.2M", "out", "139k", "cache", "30.0M"]
    assert "cache" not in fmt_tokens(Tokens(53_000, 53_000, 2000))


def test_the_spend_table_lines_up_under_a_header() -> None:
    from cabildo.usage import Spend, Window
    from cabildo.workspace.app import fmt_spend
    from cabildo.workspace.spend import Report

    s = Spend(
        today=Window(input=1_000, output=2_000, cost=0.84),
        week=Window(input=1_000_000, output=2_000_000, cost=103.9),
        all_time=Window(input=9_000_000_000, output=2_000_000, cost=4821.79),
    )
    rows = [line.split() for line in fmt_spend(Report(s))]
    assert rows[0] == ["api", "key", "spent", "tokens"]
    assert rows[1] == ["today", "$0.84", "3k"]
    assert rows[2] == ["7d", "$103.90", "3.0M"]
    assert rows[3] == ["all", "$4,821.79", "9.0B"]  # thousands and billions stay readable


def test_the_spend_table_names_tokens_it_could_not_price() -> None:
    """A model the price table does not know makes the dollar figure a floor. Saying so is
    the whole point: a cost line that quietly skipped them would read as the total.
    """
    from cabildo.usage import Spend, Window
    from cabildo.workspace.app import fmt_spend
    from cabildo.workspace.spend import Report

    priced = Window(input=1_000_000, cost=4.0)
    mixed = Window(input=3_000_000, cost=4.0, unpriced=2_000_000)
    rows = fmt_spend(Report(Spend(today=priced, week=mixed, all_time=mixed)))
    assert "unpriced" not in rows[1]
    assert rows[2].endswith("+2.0M unpriced")


def test_a_billed_figure_gets_its_own_column_and_a_gap_is_a_dash() -> None:
    """Today's billed cost lands hours late. A dash says "not known yet"; a $0.00 there
    would read as a morning that cost nothing.
    """
    from cabildo.usage import Spend, Window
    from cabildo.workspace.app import fmt_spend
    from cabildo.workspace.spend import Report

    s = Spend(
        today=Window(output=1_000, cost=241.91),
        week=Window(output=1_000, cost=4829.36),
        all_time=Window(output=1_000, cost=7440.23),
    )
    rows = [line.split() for line in fmt_spend(Report(s, None, 4712.08, 21004.55))]
    assert rows[0] == ["api", "key", "billed", "est", "tokens"]
    assert rows[1][:3] == ["today", "-", "$241.91"]
    assert rows[2][:3] == ["7d", "$4,712.08", "$4,829.36"]
    assert rows[3][:3] == ["all", "$21,004.55", "$7,440.23"]


def test_the_shown_conversation_gets_its_own_column() -> None:
    from cabildo.usage import Spend, Window
    from cabildo.workspace.app import fmt_spend
    from cabildo.workspace.spend import Report

    machine = Spend(Window(cost=307.33), Window(cost=4894.78), Window(cost=7505.65))
    mine = Spend(Window(cost=35.02), Window(cost=49.59), Window(cost=49.59))
    rows = [line.split() for line in fmt_spend(Report(machine, session=mine), width=60)]
    assert rows[0] == ["api", "key", "spent", "session", "tokens"]
    assert rows[1][:3] == ["today", "$307.33", "$35.02"]
    assert rows[3][:3] == ["all", "$7,505.65", "$49.59"]


def test_a_narrow_list_drops_columns_from_the_right_but_keeps_the_money() -> None:
    """The list can be 33 cells wide. Something has to go, and it is the rightmost
    column, never the one figure that is always known.
    """
    from cabildo.usage import Spend, Window
    from cabildo.workspace.app import fmt_spend
    from cabildo.workspace.spend import Report

    machine = Spend(Window(cost=307.33), Window(cost=4894.78), Window(cost=7505.65))
    mine = Spend(Window(cost=35.02), Window(cost=49.59), Window(cost=49.59))
    r = Report(machine, None, 4712.08, 21004.55, mine)
    narrow = fmt_spend(r, width=33)
    assert narrow[0].split() == ["api", "key", "billed", "est"]
    assert all(len(line) <= 33 for line in narrow)


def test_the_effort_line_is_empty_when_the_effort_is_unknown() -> None:
    from cabildo.workspace.app import fmt_effort
    from cabildo.workspace.sessions import Tokens

    assert fmt_effort(Tokens(1, 1, 1, effort="xhigh")).split() == ["effort", "xhigh"]
    assert fmt_effort(Tokens(1, 1, 1)) == ""


def test_the_context_line_turns_amber_then_red_as_the_window_fills() -> None:
    from cabildo.workspace.app import context_style
    from cabildo.workspace.sessions import Tokens

    def at(used: int, model: str = "claude-opus-5-5") -> str:
        return context_style(Tokens(used, used, 0, model))

    assert at(690_000) == ""
    assert "orange" in at(700_000) and "red" in at(900_000)
    assert at(950_000, model="") == ""  # window unknown


def test_R_quits_and_asks_main_for_a_reload(transcript, monkeypatch) -> None:
    transcript("s1", user("q"))

    async def scenario(app, pilot) -> None:
        await pilot.press("R")  # no Claude runs: nothing to ask
        await pilot.pause()

    app = drive(monkeypatch, scenario)
    assert app.reload_asked


def test_q_does_not_ask_for_a_reload(transcript, monkeypatch) -> None:
    transcript("s1", user("q"))

    async def scenario(app, pilot) -> None:
        await pilot.press("q")
        await pilot.pause()

    assert not drive(monkeypatch, scenario).reload_asked


def test_the_keys_screen_shows_the_version(transcript, monkeypatch) -> None:
    from cabildo import __version__

    transcript("s1", user("q"))
    seen: list[str] = []

    async def scenario(app, pilot) -> None:
        await pilot.press("question_mark")
        await pilot.pause()
        seen.append(str(app.screen.query_one("#keys", Static).render()))

    drive(monkeypatch, scenario)
    assert f"workspace {__version__}" in seen[0]
