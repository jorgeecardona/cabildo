"""The Chrome extension's local side: messages, the host, the relay, the install."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cabildo import cli
from cabildo.browser import chrome_host, chrome_install, chrome_relay, server
from cabildo.wsclient import WebSocket, WebSocketError


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A folder with a short path: a Unix socket path has about 100 characters."""
    with tempfile.TemporaryDirectory(prefix="cab") as name:
        yield Path(name)


def decode(frames: bytes) -> list[dict[str, Any]]:
    return list(chrome_host.read_native(io.BytesIO(frames)))


def test_a_message_to_chrome_round_trips() -> None:
    message = {"type": "call", "id": 1, "method": "info"}
    assert decode(b"".join(chrome_host.native_frames(message))) == [message]


def test_a_large_message_to_chrome_is_sent_in_parts_under_1_mb() -> None:
    message = {"type": "call", "id": 1, "params": 'é"\\' * 700_000}
    frames = list(chrome_host.native_frames(message))
    assert len(frames) > 1
    assert all(len(f) < 1_000_000 for f in frames)
    parts = decode(b"".join(frames))
    assert [p["index"] for p in parts] == list(range(parts[0]["count"]))
    assert json.loads("".join(p["data"] for p in parts)) == message


class Browser:
    """A host with a thread in the place of the extension: it answers the host's calls."""

    def __init__(self, directory: Path) -> None:
        to_host, self.write = os.pipe()
        self.read, from_host = os.pipe()
        self.stdin = os.fdopen(to_host, "rb")
        self.stdout = os.fdopen(from_host, "wb")
        self.extension_in = os.fdopen(self.read, "rb")
        self.extension_out = os.fdopen(self.write, "wb")
        self.calls: list[dict[str, Any]] = []
        self.clients: list[str | None] = []
        self.host = chrome_host.Host(self.stdout)
        self.path = directory / "host-1.sock"
        self.server = chrome_host.listen(self.path)
        for work in (self._accept, self._extension, self._from_chrome):
            threading.Thread(target=work, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.host.serve_client, args=(conn,), daemon=True).start()

    def _from_chrome(self) -> None:
        for message in chrome_host.read_native(self.stdin):
            self.host.from_chrome(message)

    def send(self, message: dict[str, Any]) -> None:
        for frame in chrome_host.native_frames(message):
            self.extension_out.write(frame)
        self.extension_out.flush()

    def _extension(self) -> None:
        for message in chrome_host.read_native(self.extension_in):
            if message["type"] == "client":
                self.clients.append(message["name"])
                continue
            self.calls.append(message)
            method, params = message["method"], message["params"]
            if method == "cdp.send" and params["method"] == "Network.getAllCookies":
                self.send({"type": "error", "id": message["id"], "error": "not allowed"})
                continue
            results: dict[str, Any] = {
                "info": {"version": "150.0", "userAgent": "UA"},
                "tabs.list": [7],
                "cdp.attach": {"targetInfo": {"targetId": "T7", "type": "page"}},
                "cdp.send": {"did": params.get("method")},
            }
            self.send({"type": "result", "id": message["id"], "result": results.get(method)})

    def close(self) -> None:
        self.server.close()
        self.extension_out.close()


@pytest.fixture
def browser(short_dir: Path) -> Iterator[Browser]:
    found = Browser(short_dir)
    yield found
    found.close()


def test_the_socket_opens_for_its_user_only(browser: Browser) -> None:
    assert browser.path.stat().st_mode & 0o777 == 0o600
    assert browser.path.parent.stat().st_mode & 0o777 == 0o700


def test_one_browser_server_at_a_time(browser: Browser) -> None:
    async def scenario() -> None:
        first = await chrome_relay.Extension.connect("first", browser.path.parent)
        with pytest.raises(chrome_relay.ExtensionError, match="in use by first"):
            await chrome_relay.Extension.connect("second", browser.path.parent)
        await first.close()
        for _ in range(100):
            if browser.clients[-1] is None:
                break
            await asyncio.sleep(0.01)
        await (await chrome_relay.Extension.connect("second", browser.path.parent)).close()

    asyncio.run(scenario())
    assert browser.clients[:3] == ["first", None, "second"]


def test_no_host_says_what_to_do(short_dir: Path) -> None:
    stale = short_dir / "host-2.sock"
    chrome_host.listen(stale).close()
    with pytest.raises(chrome_relay.ExtensionError, match="extension install"):
        asyncio.run(chrome_relay.Extension.connect("x", short_dir))
    assert not stale.exists()


def test_two_connected_chromes_are_refused(short_dir: Path) -> None:
    both = [chrome_host.listen(short_dir / f"host-{n}.sock") for n in (1, 2)]
    with pytest.raises(chrome_relay.ExtensionError, match="2 Chromes"):
        asyncio.run(chrome_relay.Extension.connect("x", short_dir))
    for found in both:
        found.close()


def ask(ws: WebSocket, id_: int, method: str, **more: Any) -> list[dict[str, Any]]:
    """Send a command; the messages up to and including its reply."""
    ws.send(json.dumps({"id": id_, "method": method, **more}))
    got = []
    while not got or got[-1].get("id") != id_:
        got.append(json.loads(ws.recv()))
    return got


def test_the_relay_answers_playwright(browser: Browser) -> None:
    def talk(url: str) -> None:
        ws = WebSocket.connect(url)
        assert ask(ws, 1, "Browser.getVersion")[-1]["result"]["product"] == "Chrome/150.0"
        event, reply = ask(ws, 2, "Target.setAutoAttach", params={})
        assert event["method"] == "Target.attachedToTarget" and reply["result"] == {}
        session = event["params"]["sessionId"]
        sent = ask(ws, 3, "Page.enable", sessionId=session)[-1]
        assert sent == {"id": 3, "sessionId": session, "result": {"did": "Page.enable"}}
        refused = ask(ws, 4, "Network.getAllCookies", sessionId=session)[-1]
        assert refused["error"]["message"] == "not allowed"
        assert "error" in ask(ws, 5, "Storage.getCookies")[-1]
        browser.send(
            {"type": "event", "method": "cdp.event",
             "params": {"tabId": 7, "method": "Page.loadEventFired", "params": {}}}
        )  # fmt: skip
        assert json.loads(ws.recv())["sessionId"] == session
        browser.send({"type": "event", "method": "tab.removed", "params": {"tabId": 7}})
        assert json.loads(ws.recv())["method"] == "Target.detachedFromTarget"
        ws.close()

    async def scenario() -> None:
        relay = await chrome_relay.Relay.start("t", browser.path.parent)
        try:
            await asyncio.to_thread(talk, relay.url)
        finally:
            await relay.close()

    asyncio.run(scenario())


def test_the_relay_refuses_web_pages_and_guesses(browser: Browser) -> None:
    def talk(url: str) -> None:
        with pytest.raises(WebSocketError, match="403"):
            WebSocket.connect(url, headers={"Origin": "https://evil.test"})
        with pytest.raises(WebSocketError, match="403"):
            WebSocket.connect(url.rsplit("/", 1)[0] + "/guess")

    async def scenario() -> None:
        relay = await chrome_relay.Relay.start("t", browser.path.parent)
        try:
            await asyncio.to_thread(talk, relay.url)
        finally:
            await relay.close()

    asyncio.run(scenario())


def test_the_extension_id_is_the_one_of_its_key() -> None:
    manifest = json.loads((chrome_install.SOURCE / "manifest.json").read_text())
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == chrome_install.EXTENSION_ID


def test_profile_dirs_are_the_ones_that_exist(tmp_path: Path) -> None:
    (tmp_path / ".config" / "google-chrome-unstable").mkdir(parents=True)
    mac = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    mac.mkdir(parents=True)
    assert chrome_install.profile_dirs(tmp_path, "linux") == (
        tmp_path / ".config" / "google-chrome-unstable",
    )
    assert chrome_install.profile_dirs(tmp_path, "darwin") == (mac,)


def test_install_allows_only_this_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path / "bus"))
    (tmp_path / ".config" / "chromium").mkdir(parents=True)
    monkeypatch.setattr(chrome_install.sys, "platform", "linux")
    assert cli.main(["browser", "extension", "install"]) == 0
    out = capsys.readouterr().out
    path = tmp_path / ".config/chromium/NativeMessagingHosts/dev.cabildo.chrome.json"
    manifest = json.loads(path.read_text())
    assert manifest["allowed_origins"] == [f"chrome-extension://{chrome_install.EXTENSION_ID}/"]
    script = Path(manifest["path"])
    assert os.access(script, os.X_OK)
    assert str(tmp_path / "bus") in script.read_text()
    assert (tmp_path / "bus/chrome/extension/background.js").is_file()
    assert "browser serve --chrome" in out
    assert cli.main(["browser", "extension", "status"]) == 0
    assert "not connected" in capsys.readouterr().out


def test_a_running_host_is_counted(short_dir: Path) -> None:
    live = chrome_host.listen(short_dir / "host-1.sock")
    chrome_host.listen(short_dir / "host-2.sock").close()
    assert chrome_install.running_hosts(short_dir) == 1
    live.close()


def test_chrome_mode_leaves_the_persons_chrome_alone(tmp_path: Path) -> None:
    session = server.Session(chrome=True, output_dir=tmp_path)
    mcp = server.build(session)

    async def call(name: str, **arguments: Any) -> str:
        return str(await mcp.call_tool(name, arguments))

    assert "keeps its size" in asyncio.run(call("browser_resize", width=1, height=1))
    assert "nothing to save" in asyncio.run(call("browser_save_logins", domains=["a.test"]))


def test_the_host_ends_when_chrome_closes_it(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CABILDO_HOME", str(short_dir))
    assert chrome_host.run(io.BytesIO(), io.BytesIO()) == 0
    assert not list((short_dir / "chrome").glob("*.sock"))
