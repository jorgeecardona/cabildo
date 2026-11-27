"""The extension in a real, visible Chromium: ``CABILDO_CHROME_E2E=1``, on a desktop.

Needs the Chromium of ``playwright install chromium``.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import os
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from cabildo.browser import chrome_install, chrome_relay, server

pytestmark = pytest.mark.skipif(
    os.environ.get("CABILDO_CHROME_E2E") != "1", reason="opens a browser window: set the variable"
)

PAGE = """<title>Shared</title><input aria-label="name">
<button onclick="out.textContent = 'hello ' + document.querySelector('input').value">Go</button>
<p id="out"></p><a href="two.html" target="_blank">open two</a>"""
COOKIE_COMMANDS = ("Network.getAllCookies", "Network.getCookies", "Page.getCookies")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Set-Cookie", "sid=SECRET-HTTPONLY; HttpOnly")
        super().end_headers()


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A Chromium with the extension and one tab that is not shared; the site's address."""
    with tempfile.TemporaryDirectory(prefix="cab", ignore_cleanup_errors=True) as name:
        root = Path(name)
        monkeypatch.setenv("CABILDO_HOME", str(root / "home"))
        (root / "index.html").write_text(PAGE)
        (root / "two.html").write_text("<title>Second</title>")
        (root / "mine.html").write_text("<title>Private</title>")
        handler = functools.partial(Quiet, directory=name)
        site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{site.server_address[1]}"
        profile = root / "profile"
        profile.mkdir()
        done = chrome_install.install([profile])
        with sync_playwright() as pw:
            executable = pw.chromium.executable_path
        process = subprocess.Popen(
            [
                executable,
                f"--user-data-dir={profile}",
                f"--load-extension={done.extension}",
                "--no-first-run",
                "--no-default-browser-check",
                "--password-store=basic",
                f"{base}/mine.html",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield base
        finally:
            process.terminate()
            process.wait(10)
            site.shutdown()


def test_an_agent_uses_the_shared_tabs_only(chrome: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        for _ in range(60):
            if chrome_install.running_hosts():
                break
            await asyncio.sleep(0.5)
        session = server.Session(chrome=True, output_dir=tmp_path)
        try:
            page = await session.page()
            seen: list[Any] = []

            def keep(found: Any) -> None:
                seen.append(found)

            page.on("request", keep)
            page.on("response", keep)
            await page.goto(f"{chrome}/index.html")
            await page.reload()  # this request sends the cookie
            headers = [await found.all_headers() for found in seen]
            assert headers and "SECRET-HTTPONLY" not in str(headers)
            await page.fill("input", "world")
            await page.click("text=Go")
            assert await page.inner_text("#out") == "hello world"
            async with session._context.expect_page() as opened:  # type: ignore[union-attr]
                await page.click("text=open two")
            await (await opened.value).wait_for_load_state()
            titles = sorted([await p.title() for p in await session.pages()])
            assert titles == ["Second", "Shared"]  # the private tab is not there

            extension = session._relay.extension  # type: ignore[union-attr]
            shared = await extension.call("tabs.list")
            popup = f"chrome-extension://{chrome_install.EXTENSION_ID}/popup.html"
            for url in (popup, " CHROME-EXTENSION" + popup[16:], "file:///etc/hostname",
                        "chrome://version", f"blob:{popup}", f"view-source:{popup}"):  # fmt: skip
                with pytest.raises(chrome_relay.ExtensionError, match="only web pages"):
                    await extension.call("tabs.create", url=url)
                with pytest.raises(chrome_relay.ExtensionError, match="only web pages"):
                    await extension.call(
                        "cdp.send", tabId=shared[0], method="Page.navigate", params={"url": url}
                    )
            with pytest.raises(PlaywrightError):
                await page.goto("http://nothing.invalid/")
            await asyncio.sleep(1)  # Chrome now loads its error page
            assert not page.is_closed()  # a failed load does not unshare the tab
            await page.goto(f"{chrome}/index.html")
            for command in COOKIE_COMMANDS:
                with pytest.raises(chrome_relay.ExtensionError, match="not allowed"):
                    await extension.call("cdp.send", tabId=shared[0], method=command, params={})
            # Chrome numbers tabs in order, so the private tab is just below the first shared one.
            for other in set(range(min(shared) - 6, min(shared))):
                for command in ("cdp.attach", "tabs.close", "tabs.activate"):
                    with pytest.raises(chrome_relay.ExtensionError, match="not in the"):
                        await extension.call(command, tabId=other)
        finally:
            await session.close()

    asyncio.run(asyncio.wait_for(scenario(), 90))
