"""The browser MCP server that cabildo ships, built on the Python Playwright package.

Agents call it as ``cabildo-browser``. Each server process starts one visible Chrome on its first
tool call, in a fresh in-memory context seeded from the storage-state file (see ``login``). Two
servers never share a profile, so they never contend for a lock.

The tools keep the names of the npm ``@playwright/mcp`` server. Parameter names are snake_case
(``double_click``, ``full_page``). Not implemented yet: ``browser_drag``, ``browser_drop``,
``browser_find``, ``browser_network_request`` and ``browser_run_code_unsafe``.

There is no headless mode. The browser always runs with a visible window.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    FileChooser,
    Locator,
    Page,
    Playwright,
    ViewportSize,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from cabildo import desktop
from cabildo.browser.login import write_state

_REF = re.compile(r"^(f\d+)?e\d+$")
_VIEWPORT: ViewportSize = {"width": 1280, "height": 720}
_KEEP = 500
_WAIT_MS = 30_000
_LEVEL = {"error": "error", "warning": "warning", "debug": "debug"}
_RANK = {"error": 0, "warning": 1, "info": 2, "debug": 3}
_CHALLENGE_TITLES = ("just a moment", "attention required! | cloudflare", "verifying you are human")
_CHALLENGE_HOST = "challenges.cloudflare.com"
_CF_COOKIES = ("cf_clearance", "__cf_bm")

Modifier = Literal["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]


@dataclass
class Session:
    """The one browser of this server process. It starts on first use."""

    channel: str = "chrome"
    storage_state: Path | None = None
    output_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "cabildo" / "browser")
    console: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=_KEEP))
    requests: deque[str] = field(default_factory=lambda: deque(maxlen=_KEEP))
    dialog: Dialog | None = None
    chooser: FileChooser | None = None
    current: int = 0
    _start: asyncio.Lock = field(default_factory=asyncio.Lock)
    _pw: Playwright | None = None
    _browser: Browser | None = None
    _context: BrowserContext | None = None
    _state_mtime: float = 0.0  # mtime of the storage-state file the context last loaded
    handoff_seconds: float = 120.0  # how long a Cloudflare check waits for the human

    async def context(self) -> BrowserContext:
        """The browser context, started on first use. Parallel first calls start one browser."""
        async with self._start:
            if self._context is None:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(channel=self.channel, headless=False)
                state = self.storage_state
                self._context = await self._browser.new_context(
                    viewport=_VIEWPORT,
                    storage_state=str(state) if state and state.is_file() else None,
                )
                self._context.on("page", self._watch)
                self._state_mtime = self._mtime()
            else:
                await self._reload_logins(self._context)
            return self._context

    def _mtime(self) -> float:
        state = self.storage_state
        try:
            return state.stat().st_mtime if state else 0.0
        except OSError:
            return 0.0

    async def _reload_logins(self, context: BrowserContext) -> None:
        """Add the cookies of a newer storage-state file to the running browser.

        ``cabildo browser login`` rewrites the file; without this, a browser started before
        the login stays logged out until its MCP server restarts.
        """
        mtime = self._mtime()
        if not self.storage_state or mtime <= self._state_mtime:
            return
        self._state_mtime = mtime
        try:
            cookies = json.loads(self.storage_state.read_text()).get("cookies", [])
        except (OSError, ValueError):
            return
        if cookies:
            await context.add_cookies(cookies)

    async def pages(self) -> list[Page]:
        """The open tabs. Opens one when there is none."""
        context = await self.context()
        if not context.pages:
            await context.new_page()
        return context.pages

    def ensure_no_dialog(self) -> None:
        """Raise when a dialog is open: the page cannot answer until it is handled."""
        if self.dialog is not None:
            raise DialogOpenError(dialog_message(self.dialog))

    async def page(self) -> Page:
        """The current tab. Raises ``DialogOpenError`` while a dialog is open."""
        self.ensure_no_dialog()
        pages = await self.pages()
        self.current = min(self.current, len(pages) - 1)
        return pages[self.current]

    def _watch(self, page: Page) -> None:
        """Record the console, requests, dialogs and file choosers of a tab."""
        page.on("console", lambda m: self.console.append((_LEVEL.get(m.type, "info"), m.text)))
        page.on("pageerror", lambda e: self.console.append(("error", str(e))))
        page.on(
            "response",
            lambda r: self.requests.append(f"[{r.request.method}] {r.url} => {r.status}"),
        )
        page.on("dialog", self._on_dialog)
        page.on("filechooser", self._on_chooser)

    def _on_dialog(self, dialog: Dialog) -> None:
        self.dialog = dialog

    def _on_chooser(self, chooser: FileChooser) -> None:
        self.chooser = chooser

    async def close(self) -> None:
        """Close the browser. The next tool call starts a new one.

        A browser that is already gone (crashed or reaped) does not stop the reset.
        """
        browser, pw = self._browser, self._pw
        self._pw = self._browser = self._context = None
        self.current = 0
        self.dialog = self.chooser = None
        with suppress(PlaywrightError):
            if browser is not None:
                await browser.close()
        with suppress(PlaywrightError):
            if pw is not None:
                await pw.stop()


def is_challenge(title: str, frame_urls: list[str]) -> bool:
    """Return True when the page is a Cloudflare bot check that waits for a human."""
    lowered = title.strip().lower()
    if any(lowered.startswith(t) for t in _CHALLENGE_TITLES):
        return True
    return any(_CHALLENGE_HOST in url for url in frame_urls)


def merge_cf_cookies(state: dict[str, Any], cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """Return ``state`` with the Cloudflare cookies from ``cookies`` added or replaced.

    Only ``cf_clearance`` and ``__cf_bm`` are taken: the handoff saves what the human earned
    on the check, nothing else from the agent's session.
    """
    fresh = [c for c in cookies if c.get("name") in _CF_COOKIES]
    keys = {(c["name"], c.get("domain"), c.get("path")) for c in fresh}
    kept = [
        c
        for c in state.get("cookies", [])
        if (c.get("name"), c.get("domain"), c.get("path")) not in keys
    ]
    return {**state, "cookies": kept + fresh, "origins": state.get("origins", [])}


async def _on_challenge(page: Page) -> bool:
    title = await page.title()
    return is_challenge(title, [f.url for f in page.frames])


async def handoff(session: Session, page: Page) -> str:
    """Ask the human to pass a Cloudflare check in the visible window, then keep its cookie.

    Returns a line for the agent. The agent never clicks the check itself.
    """
    host = page.url.split("/")[2] if "://" in page.url else page.url
    with suppress(PlaywrightError):
        await page.bring_to_front()
    desktop.notify(
        "Cloudflare check",
        f"An agent browser is stopped on {host}. Pass the check in its window.",
        urgent=True,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + session.handoff_seconds
    while loop.time() < deadline:
        await asyncio.sleep(1)
        with suppress(PlaywrightError):
            if not await _on_challenge(page):
                return "Cloudflare check passed by the human. " + await _save_cf(session)
    return (
        f"blocked by a Cloudflare check on {host}: nobody passed it in "
        f"{session.handoff_seconds:.0f}s. Ask the human to pass it, then navigate again."
    )


async def _save_cf(session: Session) -> str:
    path = session.storage_state
    if path is None or session._context is None:
        return "No storage-state file: the cookie lasts until this browser closes."
    try:
        current = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        current = {}
    cookies = [dict(c) for c in await session._context.cookies()]
    write_state(path, merge_cf_cookies(current, cookies))
    session._state_mtime = session._mtime()
    return f"Saved its cookie to {path}."


class DialogOpenError(RuntimeError):
    """A dialog is open. The message says what it is and which tool answers it."""


def dialog_message(dialog: Any) -> str:
    """What to tell the agent while a dialog is open."""
    return (
        f"a {dialog.type} dialog is open: {dialog.message!r}. "
        "Call browser_handle_dialog before anything else."
    )


def next_current(current: int, closed: int, remaining: int) -> int:
    """The current tab index after tab ``closed`` was closed, with ``remaining`` tabs left."""
    if closed < current:
        current -= 1
    return max(0, min(current, remaining - 1))


def locate(page: Page, target: str) -> Locator:
    """The element for ``target``: a ref from the snapshot such as ``e12``, or a selector."""
    return page.locator(f"aria-ref={target}" if _REF.match(target) else target)


async def state(session: Session) -> str:
    """The page state that most tools return: URL, title and the snapshot."""
    if session.dialog is not None:
        return dialog_message(session.dialog)
    page = await session.page()
    note = ""
    if await _on_challenge(page):
        note = f"### Note\n{await handoff(session, page)}\n"
    snapshot = await page.aria_snapshot(mode="ai")
    return (
        f"{note}### Page\n- URL: {page.url}\n- Title: {await page.title()}\n"
        f"### Snapshot\n```yaml\n{snapshot}\n```"
    )


def build(session: Session) -> FastMCP:
    """The MCP server with every tool bound to ``session``."""

    @asynccontextmanager
    async def lifespan(_app: FastMCP) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await session.close()

    mcp = FastMCP("cabildo-browser", lifespan=lifespan)

    @mcp.tool()
    async def browser_navigate(url: str) -> str:
        """Navigate to a URL."""
        page = await session.page()
        await page.goto(url)
        return await state(session)

    @mcp.tool()
    async def browser_navigate_back() -> str:
        """Go back to the previous page."""
        page = await session.page()
        await page.go_back()
        return await state(session)

    @mcp.tool()
    async def browser_snapshot(
        target: str | None = None,
        depth: int | None = None,
        boxes: bool = False,
        filename: str | None = None,
    ) -> str:
        """Capture the accessibility snapshot of the page. Better than a screenshot."""
        page = await session.page()
        if target:
            text = await locate(page, target).aria_snapshot(mode="ai", depth=depth, boxes=boxes)
        else:
            text = await page.aria_snapshot(mode="ai", depth=depth, boxes=boxes)
        if filename:
            return _save_text(session, filename, text)
        return text

    @mcp.tool()
    async def browser_click(
        target: str,
        element: str | None = None,
        button: Literal["left", "right", "middle"] = "left",
        double_click: bool = False,
        modifiers: list[Modifier] | None = None,
    ) -> str:
        """Click an element. ``target`` is a ref from the snapshot or a selector."""
        page = await session.page()
        loc = locate(page, target)
        if double_click:
            await loc.dblclick(button=button, modifiers=modifiers)
        else:
            await loc.click(button=button, modifiers=modifiers)
        return await state(session)

    @mcp.tool()
    async def browser_type(
        target: str,
        text: str,
        element: str | None = None,
        submit: bool = False,
        slowly: bool = False,
    ) -> str:
        """Type text into an editable element."""
        page = await session.page()
        loc = locate(page, target)
        if slowly:
            await loc.press_sequentially(text)
        else:
            await loc.fill(text)
        if submit:
            await loc.press("Enter")
        return await state(session)

    @mcp.tool()
    async def browser_fill_form(fields: list[dict[str, str]]) -> str:
        """Fill several form fields. Each field has target, name, type and value.

        ``type`` is textbox, checkbox, radio, combobox or slider.
        """
        page = await session.page()
        for item in fields:
            loc = locate(page, item["target"])
            kind, value = item["type"], item["value"]
            if kind in {"checkbox", "radio"}:
                await loc.set_checked(value.lower() == "true")
            elif kind == "combobox":
                await loc.select_option(label=value)
            else:
                await loc.fill(value)
        return await state(session)

    @mcp.tool()
    async def browser_press_key(key: str) -> str:
        """Press a key such as ``ArrowLeft`` or ``Enter``."""
        page = await session.page()
        await page.keyboard.press(key)
        return await state(session)

    @mcp.tool()
    async def browser_hover(target: str, element: str | None = None) -> str:
        """Hover over an element."""
        page = await session.page()
        await locate(page, target).hover()
        return await state(session)

    @mcp.tool()
    async def browser_select_option(
        target: str, values: list[str], element: str | None = None
    ) -> str:
        """Select options in a dropdown."""
        page = await session.page()
        await locate(page, target).select_option(values)
        return await state(session)

    @mcp.tool(structured_output=False)
    async def browser_take_screenshot(
        target: str | None = None,
        element: str | None = None,
        type: Literal["png", "jpeg"] = "png",
        filename: str | None = None,
        full_page: bool = False,
    ) -> Image | str:
        """Take a screenshot. To act on the page, use browser_snapshot."""
        page = await session.page()
        if target:
            data = await locate(page, target).screenshot(type=type)
        else:
            data = await page.screenshot(type=type, full_page=full_page)
        if filename:
            return _save_bytes(session, filename, data)
        return Image(data=data, format=type)

    @mcp.tool()
    async def browser_evaluate(
        function: str,
        target: str | None = None,
        element: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Evaluate a JavaScript function on the page, or on an element when ``target`` is set."""
        page = await session.page()
        if target:
            result: Any = await locate(page, target).evaluate(function)
        else:
            result = await page.evaluate(function)
        text = json.dumps(result, default=str, indent=2)
        if filename:
            return _save_text(session, filename, text)
        return text

    @mcp.tool()
    async def browser_wait_for(
        time: float | None = None, text: str | None = None, text_gone: str | None = None
    ) -> str:
        """Wait for text to appear, text to disappear, or a number of seconds."""
        page = await session.page()
        if time:
            await asyncio.sleep(time)
        if text:
            await page.get_by_text(text).first.wait_for(state="visible", timeout=_WAIT_MS)
        if text_gone:
            await page.get_by_text(text_gone).first.wait_for(state="hidden", timeout=_WAIT_MS)
        return await state(session)

    @mcp.tool()
    async def browser_tabs(
        action: Literal["list", "new", "close", "select"],
        index: int | None = None,
        url: str | None = None,
    ) -> str:
        """List, create, close or select a tab."""
        context = await session.context()
        pages = await session.pages()
        if action == "new":
            page = await context.new_page()
            if url:
                await page.goto(url)
            session.current = len(context.pages) - 1
        elif action == "close":
            at = session.current if index is None else index
            if not 0 <= at < len(pages):
                return f"no tab {at}: there are {len(pages)}"
            await pages[at].close()
            session.current = next_current(session.current, at, len(context.pages))
        elif action == "select":
            if index is None or not 0 <= index < len(pages):
                return f"select needs an index from 0 to {len(pages) - 1}"
            session.current = index
            await pages[index].bring_to_front()
        listing = []
        for i, page in enumerate(await session.pages()):
            mark = " (current)" if i == session.current else ""
            listing.append(f"- {i}:{mark} [{await page.title()}]({page.url})")
        return "\n".join(listing)

    @mcp.tool()
    async def browser_console_messages(
        level: Literal["error", "warning", "info", "debug"] = "info",
    ) -> str:
        """Console messages since the browser started, at ``level`` or more severe."""
        return "\n".join(
            f"[{lvl.upper()}] {text}" for lvl, text in session.console if _RANK[lvl] <= _RANK[level]
        )

    @mcp.tool()
    async def browser_network_requests(static: bool = False) -> str:
        """Responses seen since the browser started. Static files are left out by default."""
        skip = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".woff", ".woff2", ".ico")
        return "\n".join(
            r
            for r in session.requests
            if static or not r.split(" ")[1].split("?")[0].endswith(skip)
        )

    @mcp.tool()
    async def browser_handle_dialog(accept: bool, prompt_text: str | None = None) -> str:
        """Accept or dismiss the open dialog."""
        dialog = session.dialog
        if dialog is None:
            return "no dialog is open"
        session.dialog = None
        if accept:
            await dialog.accept(prompt_text)
        else:
            await dialog.dismiss()
        return await state(session)

    @mcp.tool()
    async def browser_file_upload(paths: list[str] | None = None) -> str:
        """Give the open file chooser these files, or cancel it when ``paths`` is empty."""
        chooser = session.chooser
        if chooser is None:
            return "no file chooser is open"
        session.chooser = None
        await chooser.set_files(paths or [])
        return await state(session)

    @mcp.tool()
    async def browser_resize(width: int, height: int) -> str:
        """Resize the browser viewport."""
        page = await session.page()
        await page.set_viewport_size({"width": width, "height": height})
        return await state(session)

    @mcp.tool()
    async def browser_close() -> str:
        """Close the browser. The next call opens a new one."""
        await session.close()
        return "browser closed"

    return mcp


def _save_text(session: Session, filename: str, text: str) -> str:
    """Write ``text`` under the output directory and say where."""
    return _save_bytes(session, filename, text.encode())


def _save_bytes(session: Session, filename: str, data: bytes) -> str:
    """Write ``data`` under the output directory. A name that leaves the directory is refused."""
    root = session.output_dir.resolve()
    target = (root / filename).resolve()
    if root not in target.parents:
        return f"refused: {filename} is outside {root}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"saved {target}"


def serve(session: Session) -> int:
    """Run the server on stdio until the client closes it."""
    build(session).run()
    return 0
