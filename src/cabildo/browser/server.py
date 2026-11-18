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
import fcntl
import json
import re
import sys
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
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
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from cabildo import config, desktop, onepassword
from cabildo.browser import credentials
from cabildo.browser.entry import STALE_DAYS, read_storage_state
from cabildo.browser.login import write_state

_REF = re.compile(r"^(f\d+)?e\d+$")
# Sites with a bot check (Hetzner's Heray) refuse a browser that reports
# ``navigator.webdriver`` or a screen the size of the viewport. The window keeps its real size
# and the real screen; only the automation marks go.
_WINDOW = "--window-size=1280,800"
_LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled", _WINDOW)
_NOT_DEFAULT_ARGS = ("--enable-automation",)
_KEEP = 500
_WAIT_MS = 30_000
_LEVEL = {"error": "error", "warning": "warning", "debug": "debug"}
_RANK = {"error": 0, "warning": 1, "info": 2, "debug": 3}
_CHALLENGE_TITLES = ("just a moment", "attention required! | cloudflare", "verifying you are human")
_CHALLENGE_HOST = "challenges.cloudflare.com"
_CF_COOKIES = ("cf_clearance", "__cf_bm")
_CONCEALED = "<concealed>"
_TEXT_FIELDS = ("INPUT", "TEXTAREA")
# The document origin and kind of the element a login value would go into.
_FIELD_INFO = "el => [el.ownerDocument.location.origin, el.tagName, el.type || '']"

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
    logins: Mapping[str, credentials.Credential] = field(default_factory=dict)
    read_secret: Callable[[str], onepassword.Secret] = onepassword.read
    # Secrets filled by browser_login that are still in the page. Cleared on navigation.
    concealed: set[str] = field(default_factory=set)
    _login_notice_given: bool = False

    def login_notice(self) -> str | None:
        """A line when the saved logins are missing, empty or old. Said once per server (#49)."""
        if self._login_notice_given or self.storage_state is None:
            return None
        self._login_notice_given = True
        saved = read_storage_state(self.storage_state)
        if not saved.exists:
            problem = "there is no saved login file"
        elif saved.empty:
            problem = "the saved login file holds no login"
        elif saved.stale:
            problem = f"the saved logins are {saved.age_days:.0f} days old (over {STALE_DAYS})"
        else:
            return None
        return (
            f"{problem}, so sites that need a login may fail. Ask the human to run "
            "`cabildo browser login`. A login the human makes in this browser can be kept with "
            "browser_save_logins."
        )

    async def context(self) -> BrowserContext:
        """The browser context, started on first use. Parallel first calls start one browser."""
        async with self._start:
            if self._context is None:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    channel=self.channel,
                    headless=False,
                    args=list(_LAUNCH_ARGS),
                    ignore_default_args=list(_NOT_DEFAULT_ARGS),
                )
                state = self.storage_state
                self._context = await self._browser.new_context(
                    no_viewport=True,
                    storage_state=str(state) if state and state.is_file() else None,
                )
                self._context.on("page", self._watch)
                self._state_mtime = self._mtime()
            else:
                await self._reload_logins(self._context)
            return self._context

    def redact(self, text: str) -> str:
        """``text`` with every concealed secret replaced, longest first."""
        for value in sorted(self.concealed, key=len, reverse=True):
            text = text.replace(value, _CONCEALED)
        return text

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
        page.on("framenavigated", self._on_navigated)

    def _on_navigated(self, frame: Any) -> None:
        # The form was sent or the page left: the secret is no longer in this document.
        if frame.parent_frame is None:
            self.concealed.clear()

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
        self.concealed.clear()
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


def _host_matches(host: str, domains: Sequence[str]) -> bool:
    """True when ``host`` is one of ``domains`` or a subdomain of one."""
    host = host.lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def normalize_domains(domains: Sequence[str]) -> list[str]:
    """``https://www.Example.com/x`` and ``.example.com`` both become a bare lowercase host."""
    out: list[str] = []
    for raw in domains:
        host = raw.strip().lower().split("://")[-1].split("/")[0].split(":")[0].lstrip(".")
        if host and host not in out:
            out.append(host)
    return out


def merge_logins(
    saved: dict[str, Any], fresh: dict[str, Any], domains: Sequence[str]
) -> dict[str, Any]:
    """``saved`` with the cookies and origins of ``domains`` taken from ``fresh``.

    A cookie is replaced by the one with the same name, domain and path; every other saved entry
    is kept, so one browser's save never removes the logins another one made (#49).
    """
    cookies = [
        c for c in fresh.get("cookies", []) if _host_matches(str(c.get("domain", "")), domains)
    ]
    keys = {(c["name"], c.get("domain"), c.get("path")) for c in cookies}
    kept = [
        c
        for c in saved.get("cookies", [])
        if (c.get("name"), c.get("domain"), c.get("path")) not in keys
    ]
    origins = [
        o
        for o in fresh.get("origins", [])
        if _host_matches(str(o.get("origin", "")).split("://")[-1].split(":")[0], domains)
    ]
    seen = {o.get("origin") for o in origins}
    kept_origins = [o for o in saved.get("origins", []) if o.get("origin") not in seen]
    return {**saved, "cookies": kept + cookies, "origins": kept_origins + origins}


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """An exclusive lock beside ``path``: several browsers run at once, each may save."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def save_logins(session: Session, domains: Sequence[str]) -> str:
    """Merge this browser's logins for ``domains`` into the saved login file."""
    path = session.storage_state
    hosts = normalize_domains(domains)
    if not hosts:
        return "Name at least one domain, for example `splitwise.com`."
    if path is None:
        return "No storage-state file is set for this server: nothing to save to."
    if session._context is None:
        return "This browser has not been used yet: there is no login to save."
    fresh = dict(await session._context.storage_state())
    with _locked(path):
        try:
            current = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, ValueError):
            current = {}
        merged = merge_logins(current, fresh, hosts)
        added = sum(
            1 for c in fresh.get("cookies", []) if _host_matches(str(c.get("domain", "")), hosts)
        )
        if added == 0:
            return f"No cookie for {', '.join(hosts)} in this browser: nothing saved."
        write_state(path, merged)
    session._state_mtime = session._mtime()
    return f"Saved {added} cookie(s) for {', '.join(hosts)} to {path}."


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
    notes = []
    if await _on_challenge(page):
        notes.append(await handoff(session, page))
    if notice := session.login_notice():
        notes.append(notice)
    note = "".join(f"### Note\n{n}\n" for n in notes)
    snapshot = await page.aria_snapshot(mode="ai")
    return session.redact(
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
    async def browser_save_logins(domains: list[str]) -> str:
        """Keep this browser's logins for the named sites in the saved login file.

        Use it after the human has logged in here, two-step verification included, so later
        browsers start logged in. Name each site by its domain, for example `splitwise.com`.
        Only cookies of those domains and their subdomains are saved; nothing else is touched.
        """
        return await save_logins(session, domains)

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
    async def browser_login(
        site: str,
        username_target: str | None = None,
        password_target: str | None = None,
        otp_target: str | None = None,
        submit: bool = False,
    ) -> str:
        """Fill a login form from 1Password. The secret never comes back to you.

        ``site`` is a name from the human's ``[browser.login.<site>]`` config. The fields are
        snapshot refs, and each one is optional: give only the fields on the page. A site that
        asks for the one-time code on a second page needs a second call with only
        ``otp_target``. The page must be on the site's origin, or nothing is read or filled.
        Set ``submit`` to press Enter in the last field filled.
        """
        return await login(session, site, username_target, password_target, otp_target, submit)

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
        if session.concealed:
            return (
                "refused: a login secret is in the page. Submit the form or navigate first: "
                "script could read it."
            )
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
        return session.redact(
            "\n".join(
                f"[{lvl.upper()}] {text}"
                for lvl, text in session.console
                if _RANK[lvl] <= _RANK[level]
            )
        )

    @mcp.tool()
    async def browser_network_requests(static: bool = False) -> str:
        """Responses seen since the browser started. Static files are left out by default."""
        skip = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".woff", ".woff2", ".ico")
        return session.redact(
            "\n".join(
                r
                for r in session.requests
                if static or not r.split(" ")[1].split("?")[0].endswith(skip)
            )
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


async def _check_field(
    cred: credentials.Credential, loc: Locator, *, password: bool, otp: bool = False
) -> None:
    """Raise unless ``loc`` is a text field of the right kind in a document on the origin."""
    origin, tag, kind = await loc.evaluate(_FIELD_INFO)
    credentials.check_origin(cred, origin)
    if tag not in _TEXT_FIELDS:
        msg = "the target is not a text field"
        raise credentials.CredentialError(msg)
    if password and kind != "password":
        msg = "the password target is not a password field"
        raise credentials.CredentialError(msg)
    if not (password or otp) and kind == "password":
        msg = "the username target is a password field"
        raise credentials.CredentialError(msg)


async def login(
    session: Session,
    site: str,
    username_target: str | None,
    password_target: str | None,
    otp_target: str | None,
    submit: bool,
) -> str:
    """Fill the fields of the login form for ``site``. Returns the page state, or ``refused: ...``.

    Each target is optional, so a login that spans two pages is two calls. At least one is needed.

    The origin of the page and of each field is checked before ``op`` is called, and again just
    before each fill. The password and the code are concealed from every tool result until the
    page navigates.
    """
    cred = session.logins.get(site)
    if cred is None:
        known = ", ".join(sorted(session.logins)) or "none"
        return f"refused: no login for {site!r}. Configured sites: {known}"
    if otp_target and not cred.otp:
        return f"refused: {site} has no otp reference"
    # (target, reference, is_password, is_otp), in the order the fields are filled
    plan: list[tuple[str, str, bool, bool]] = []
    if username_target:
        plan.append((username_target, cred.username, False, False))
    if password_target:
        plan.append((password_target, cred.password, True, False))
    if otp_target and cred.otp:
        plan.append((otp_target, cred.otp, False, True))
    if not plan:
        return "refused: give at least one of username_target, password_target, otp_target"
    page = await session.page()
    try:
        credentials.check_origin(cred, page.url)
        for target, _ref, is_password, is_otp in plan:
            await _check_field(cred, locate(page, target), password=is_password, otp=is_otp)
        # op can wait on a human (unlock, biometrics), so the page may have moved by now.
        values = [await asyncio.to_thread(session.read_secret, ref) for _t, ref, _p, _o in plan]
        for (target, _ref, is_password, is_otp), secret in zip(plan, values, strict=True):
            loc = locate(page, target)
            credentials.check_origin(cred, page.url)
            await _check_field(cred, loc, password=is_password, otp=is_otp)
            if is_password or is_otp:
                session.concealed.add(secret.reveal())
            await loc.fill(secret.reveal())
    except credentials.CredentialError as exc:
        return f"refused: {exc}"
    except onepassword.OpError as exc:
        return f"refused: could not read from 1Password: {exc}"
    if submit:
        await locate(page, plan[-1][0]).press("Enter")
    return f"filled {site} from 1Password\n{await state(session)}"


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
    """Run the server on stdio until the client closes it.

    The login table is read from the machine-wide config unless the session already has one.
    """
    if not session.logins:
        table = credentials.load(config.browser_login_tables())
        session.logins = table.by_site
        for problem in table.problems:
            print(f"cabildo-browser: skipped login {problem}", file=sys.stderr)
    build(session).run()
    return 0
