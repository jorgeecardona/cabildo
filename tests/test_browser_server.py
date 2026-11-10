"""The cabildo-browser MCP server: tools, target resolution, files, and the headed-only rule."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import pytest
from mcp.server.fastmcp import FastMCP
from playwright.async_api import Error as PlaywrightError

from cabildo import cli
from cabildo.browser import server
from cabildo.browser.server import Session

T = TypeVar("T")


class FakeLocator:
    def __init__(self, selector: str, log: list[tuple[str, ...]]) -> None:
        self.selector = selector
        self.log = log

    async def click(self, **kw: Any) -> None:
        self.log.append(("click", self.selector, str(kw["button"]), str(kw["modifiers"])))

    async def dblclick(self, **kw: Any) -> None:
        self.log.append(("dblclick", self.selector, str(kw["button"]), str(kw["modifiers"])))

    async def fill(self, text: str) -> None:
        self.log.append(("fill", self.selector, text))

    async def press(self, key: str) -> None:
        self.log.append(("press", self.selector, key))

    async def set_checked(self, value: bool) -> None:
        self.log.append(("set_checked", self.selector, str(value)))

    async def select_option(self, *args: Any, **kw: Any) -> None:
        self.log.append(("select_option", self.selector, str(args or kw)))

    async def screenshot(self, **_kw: Any) -> bytes:
        return b"element-png"


class FakePage:
    url = "https://example.test/"

    def __init__(self) -> None:
        self.log: list[tuple[str, ...]] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector, self.log)

    async def title(self) -> str:
        return "Example"

    async def aria_snapshot(self, **kw: Any) -> str:
        self.log.append(("aria_snapshot", str(kw.get("mode"))))
        return '- button "Go" [ref=e2]'

    async def goto(self, url: str) -> None:
        self.log.append(("goto", url))

    async def screenshot(self, **_kw: Any) -> bytes:
        return b"page-png"


class FakeSession(Session):
    """A session whose page is a recorder, so no browser starts."""

    def __post_init__(self) -> None:
        self.fake = FakePage()
        self.closed = 0

    async def page(self) -> Any:
        return self.fake

    async def close(self) -> None:
        self.closed += 1


def make(tmp_path: Path) -> tuple[FakeSession, FastMCP]:
    session = FakeSession(output_dir=tmp_path / "out")
    session.__post_init__()
    return session, server.build(session)


def run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


def call(mcp: FastMCP, name: str, args: dict[str, Any] | None = None) -> str:
    out = run(mcp.call_tool(name, args or {}))
    blocks = out[0] if isinstance(out, tuple) else out
    return blocks[0].text  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("target", "selector"),
    [("e12", "aria-ref=e12"), ("f1e3", "aria-ref=f1e3"), ("#q", "#q"), ("button.go", "button.go")],
)
def test_a_snapshot_ref_becomes_an_aria_ref_and_anything_else_is_a_selector(
    target: str, selector: str
) -> None:
    assert server.locate(FakePage(), target).selector == selector  # type: ignore[arg-type,attr-defined]


def test_the_server_offers_the_browser_tools_and_nothing_headless(tmp_path: Path) -> None:
    _session, mcp = make(tmp_path)
    names = {t.name for t in run(mcp.list_tools())}
    assert {"browser_navigate", "browser_snapshot", "browser_click", "browser_type"} <= names
    assert "browser_close" in names
    assert not any("headless" in n for n in names)


def test_navigate_goes_to_the_url_and_returns_the_page_state(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    text = call(mcp, "browser_navigate", {"url": "https://a.test/"})
    assert ("goto", "https://a.test/") in session.fake.log
    assert "- URL: https://example.test/" in text
    assert "- Title: Example" in text
    assert '- button "Go" [ref=e2]' in text
    assert ("aria_snapshot", "ai") in session.fake.log


def test_click_by_ref_uses_the_aria_ref_locator(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    call(mcp, "browser_click", {"target": "e2"})
    assert ("click", "aria-ref=e2", "left", "None") in session.fake.log


def test_double_click_with_a_modifier(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    call(mcp, "browser_click", {"target": "e2", "double_click": True, "modifiers": ["Shift"]})
    assert ("dblclick", "aria-ref=e2", "left", "['Shift']") in session.fake.log


def test_type_with_submit_fills_then_presses_enter(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    call(mcp, "browser_type", {"target": "e3", "text": "hello", "submit": True})
    assert session.fake.log[:2] == [
        ("fill", "aria-ref=e3", "hello"),
        ("press", "aria-ref=e3", "Enter"),
    ]


def test_fill_form_picks_the_action_for_each_field_type(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    fields = [
        {"target": "e1", "name": "n", "type": "textbox", "value": "abc"},
        {"target": "e2", "name": "c", "type": "checkbox", "value": "true"},
        {"target": "e3", "name": "s", "type": "combobox", "value": "Red"},
    ]
    call(mcp, "browser_fill_form", {"fields": fields})
    kinds = [entry[0] for entry in session.fake.log if entry[0] != "aria_snapshot"]
    assert kinds == ["fill", "set_checked", "select_option"]


def test_console_messages_keep_the_level_asked_and_the_more_severe(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    session.console.extend(
        [("error", "boom"), ("warning", "careful"), ("info", "hi"), ("debug", "x")]
    )
    assert (
        call(mcp, "browser_console_messages", {"level": "warning"})
        == "[ERROR] boom\n[WARNING] careful"
    )
    assert "[DEBUG] x" in call(mcp, "browser_console_messages", {"level": "debug"})


def test_network_requests_leave_out_static_files_unless_asked(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    session.requests.extend(
        ["[GET] https://a.test/api/x => 200", "[GET] https://a.test/app.js?v=1 => 200"]
    )
    assert call(mcp, "browser_network_requests") == "[GET] https://a.test/api/x => 200"
    assert "app.js" in call(mcp, "browser_network_requests", {"static": True})


def test_a_screenshot_with_a_filename_is_saved_inside_the_output_directory(tmp_path: Path) -> None:
    _session, mcp = make(tmp_path)
    text = call(mcp, "browser_take_screenshot", {"filename": "shots/a.png"})
    saved = tmp_path / "out" / "shots" / "a.png"
    assert saved.read_bytes() == b"page-png"
    assert str(saved.resolve()) in text


def test_a_filename_that_leaves_the_output_directory_is_refused(tmp_path: Path) -> None:
    _session, mcp = make(tmp_path)
    text = call(mcp, "browser_take_screenshot", {"filename": "../escape.png"})
    assert text.startswith("refused")
    assert not (tmp_path / "escape.png").exists()


def test_handle_dialog_without_a_dialog_says_so(tmp_path: Path) -> None:
    _session, mcp = make(tmp_path)
    assert call(mcp, "browser_handle_dialog", {"accept": True}) == "no dialog is open"


def test_handle_dialog_accepts_the_pending_dialog_once(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    seen: list[str] = []

    class Dialog:
        async def accept(self, prompt: str | None = None) -> None:
            seen.append(f"accept:{prompt}")

        async def dismiss(self) -> None:
            seen.append("dismiss")

    session.dialog = Dialog()  # type: ignore[assignment]
    call(mcp, "browser_handle_dialog", {"accept": True, "prompt_text": "yes"})
    assert seen == ["accept:yes"]
    assert session.dialog is None


def test_close_closes_the_session(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    assert call(mcp, "browser_close") == "browser closed"
    assert session.closed == 1


def test_serve_refuses_headless_and_a_profile_directory(capsys) -> None:
    assert cli.main(["browser", "serve", "--headless"]) == 2
    assert cli.main(["browser", "serve", "--user-data-dir", "/p"]) == 2
    assert "always visible" in capsys.readouterr().err


class FakeTab:
    def __init__(self, title: str, log: list[str]) -> None:
        self._title = title
        self.url = f"https://{title}.test/"
        self.log = log

    async def title(self) -> str:
        return self._title

    async def close(self) -> None:
        self.log.append(f"close {self._title}")

    async def bring_to_front(self) -> None:
        self.log.append(f"front {self._title}")


class TabSession(FakeSession):
    """A fake session with three tabs, to test tab handling."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.log: list[str] = []
        self.tabs = [FakeTab(name, self.log) for name in ("a", "b", "c")]

    async def context(self) -> Any:
        tabs = self.tabs

        class Context:
            pages = tabs

        return Context()

    async def pages(self) -> list[Any]:
        return self.tabs


def tab_server(tmp_path: Path) -> tuple[TabSession, FastMCP]:
    session = TabSession(output_dir=tmp_path / "out")
    session.__post_init__()
    return session, server.build(session)


@pytest.mark.parametrize(
    ("current", "closed", "remaining", "expected"),
    [(1, 0, 2, 0), (1, 2, 2, 1), (2, 2, 2, 1), (0, 0, 2, 0), (0, 0, 1, 0), (1, 1, 2, 1)],
)
def test_the_current_tab_follows_the_tab_that_was_current(
    current: int, closed: int, remaining: int, expected: int
) -> None:
    assert server.next_current(current, closed, remaining) == expected


def test_select_with_a_bad_index_changes_nothing(tmp_path: Path) -> None:
    session, mcp = tab_server(tmp_path)
    session.current = 1
    assert call(mcp, "browser_tabs", {"action": "select", "index": 9}).startswith("select needs")
    assert call(mcp, "browser_tabs", {"action": "select", "index": -1}).startswith("select needs")
    assert session.current == 1


def test_closing_a_tab_that_does_not_exist_is_a_message(tmp_path: Path) -> None:
    session, mcp = tab_server(tmp_path)
    assert call(mcp, "browser_tabs", {"action": "close", "index": 5}) == "no tab 5: there are 3"
    assert session.log == []


class FakeChromium:
    def __init__(self, counter: list[int]) -> None:
        self.counter = counter

    async def launch(self, **_kw: Any) -> Any:
        await asyncio.sleep(0.05)
        self.counter.append(1)
        return FakeBrowser()


class FakeBrowser:
    async def new_context(self, **_kw: Any) -> Any:
        class Context:
            pages: ClassVar[list[Any]] = []

            def on(self, *_a: Any) -> None:
                return None

        return Context()


class FakePlaywright:
    def __init__(self, counter: list[int]) -> None:
        self.chromium = FakeChromium(counter)

    async def stop(self) -> None:
        return None


def test_parallel_first_calls_start_one_browser(monkeypatch) -> None:
    launches: list[int] = []

    class Starter:
        async def start(self) -> FakePlaywright:
            return FakePlaywright(launches)

    monkeypatch.setattr(server, "async_playwright", Starter)

    async def both() -> None:
        session = Session()
        await asyncio.gather(session.context(), session.context())

    run(both())
    assert len(launches) == 1


def test_close_resets_the_session_even_when_the_browser_is_already_gone() -> None:
    class Dead:
        async def close(self) -> None:
            msg = "browser has been closed"
            raise PlaywrightError(msg)

    session = Session()
    session._browser = Dead()  # type: ignore[assignment]
    session._context = object()  # type: ignore[assignment]
    session.current = 2
    run(session.close())
    assert session._context is None
    assert session.current == 0


class OpenDialog:
    type = "alert"
    message = "Sure?"


def test_an_open_dialog_stops_page_calls_with_a_message_that_names_the_tool() -> None:
    session = Session()
    session.dialog = OpenDialog()  # type: ignore[assignment]
    with pytest.raises(server.DialogOpenError, match="browser_handle_dialog"):
        session.ensure_no_dialog()


def test_state_reports_an_open_dialog_instead_of_asking_the_blocked_page(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    session.dialog = OpenDialog()  # type: ignore[assignment]
    text = call(mcp, "browser_click", {"target": "e2"})
    assert "a alert dialog is open: 'Sure?'" in text
    assert ("aria_snapshot", "ai") not in session.fake.log


class CookieContext:
    def __init__(self) -> None:
        self.added: list[list[dict[str, object]]] = []

    async def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.added.append(cookies)


def test_a_newer_login_file_reaches_a_running_browser_once(tmp_path: Path) -> None:
    import asyncio
    import json
    import os

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [{"name": "SID", "value": "1"}]}))
    session = Session(storage_state=state, output_dir=tmp_path / "out")
    session._state_mtime = state.stat().st_mtime  # the browser started from this file
    context = CookieContext()

    asyncio.run(session._reload_logins(context))  # type: ignore[arg-type]
    assert context.added == []  # same file: nothing to add
    later = state.stat().st_mtime + 10
    os.utime(state, (later, later))  # cabildo browser login rewrote it
    asyncio.run(session._reload_logins(context))  # type: ignore[arg-type]
    asyncio.run(session._reload_logins(context))  # type: ignore[arg-type]
    assert context.added == [[{"name": "SID", "value": "1"}]]
