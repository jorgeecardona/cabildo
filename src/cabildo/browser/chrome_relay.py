"""Lets Playwright drive the person's Chrome through the cabildo extension.

Playwright's ``connect_over_cdp`` expects a browser's DevTools WebSocket. ``Relay`` is that
WebSocket, on a random local port and path, for one connection. It answers the browser-level
commands itself and sends the commands of a tab to the extension, over the socket of
``chrome_host``. The extension only reaches the tabs of its ``cabildo`` tab group, so those
are the only tabs Playwright sees.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import struct
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cabildo.browser.chrome_host import socket_dir

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT, _CLOSE, _PING, _PONG = 0x1, 0x8, 0x9, 0xA
_MAX_BYTES = 256 * 1024 * 1024  # a full-page screenshot is one message
_HELLO_SECONDS = 5.0
# Browser-level commands Playwright sends that need no action in the person's Chrome.
_IGNORED = frozenset({"Browser.setDownloadBehavior"})
NOT_CONNECTED = (
    "the cabildo extension is not connected. In Chrome, open the extension's popup: it says "
    "why. To install it, run `cabildo browser extension install`."
)


class ExtensionError(RuntimeError):
    """The extension is not reachable, or it refused a command."""


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes]:
    """One WebSocket frame from a client: opcode, final flag, unmasked payload."""
    b0, b1 = await reader.readexactly(2)
    length = b1 & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack(">Q", await reader.readexactly(8))
    if length > _MAX_BYTES:
        raise ConnectionError("frame too large")
    mask = await reader.readexactly(4) if b1 & 0x80 else b""
    data = await reader.readexactly(length)
    if mask and data:
        key = (mask * (length // 4 + 1))[:length]
        data = (int.from_bytes(data) ^ int.from_bytes(key)).to_bytes(length)
    return b0 & 0x0F, bool(b0 & 0x80), data


def server_frame(opcode: int, payload: bytes) -> bytes:
    """One final, unmasked WebSocket frame."""
    length = len(payload)
    if length < 126:
        head = bytes([0x80 | opcode, length])
    elif length < 1 << 16:
        head = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
    else:
        head = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)
    return head + payload


class Extension:
    """The connection to a running host, and through it to the extension."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._calls: dict[int, asyncio.Future[Any]] = {}
        self._next = 0
        self.events: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, name: str, directory: Path | None = None) -> Extension:
        """Connect to the running host.

        Raises:
            ExtensionError: no host runs, it is in use, or several Chromes are connected, so
                which one the person means is not known.
        """
        live: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        for path in (directory or socket_dir()).glob("host-*.sock"):
            try:
                live.append(await asyncio.open_unix_connection(str(path), limit=_MAX_BYTES))
            except ConnectionRefusedError:
                path.unlink(missing_ok=True)  # its host is gone
            except OSError:
                continue
        if len(live) != 1:
            for _, writer in live:
                writer.close()
            if not live:
                raise ExtensionError(NOT_CONNECTED)
            raise ExtensionError(
                f"the extension is connected in {len(live)} Chromes or profiles. Turn its "
                "switch off in all but the one agents may use."
            )
        reader, writer = live[0]
        writer.write(json.dumps({"name": name}).encode() + b"\n")
        try:
            reply = json.loads(await asyncio.wait_for(reader.readline(), _HELLO_SECONDS) or b"{}")
        except (TimeoutError, OSError, ValueError):
            reply = {}
        if reply.get("type") == "welcome":
            return cls(reader, writer)
        writer.close()
        raise ExtensionError(reply.get("reason", NOT_CONNECTED))

    async def _read(self) -> None:
        try:
            while line := await self._reader.readline():
                message = json.loads(line)
                if message["type"] == "event":
                    self.events.put_nowait((message["method"], message.get("params", {})))
                    continue
                future = self._calls.pop(message["id"], None)
                if future is None or future.done():
                    continue
                if message["type"] == "error":
                    future.set_exception(ExtensionError(message["error"]))
                else:
                    future.set_result(message.get("result"))
        finally:
            gone = ExtensionError("the connection to the extension closed")
            for future in self._calls.values():
                if not future.done():
                    future.set_exception(gone)
            self._calls.clear()
            self.events.put_nowait(None)

    async def call(self, command: str, /, **params: Any) -> Any:
        """The result of one command of the extension.

        Raises:
            ExtensionError: it refused, or the connection closed.
        """
        if self._task.done():
            raise ExtensionError("the connection to the extension closed")
        self._next += 1
        future = self._calls[self._next] = asyncio.get_running_loop().create_future()
        self._writer.write(
            json.dumps({"id": self._next, "method": command, "params": params}).encode() + b"\n"
        )
        return await future

    async def close(self) -> None:
        """Drop the connection. The extension then detaches from every tab."""
        self._writer.close()
        self._task.cancel()
        with suppress(asyncio.CancelledError, OSError, ValueError):
            await self._task


@dataclass
class _Tab:
    tab_id: int
    session_id: str
    target: dict[str, Any]
    children: set[str] = field(default_factory=set)  # sessions of its frames and workers


class Relay:
    """The DevTools WebSocket Playwright connects to. ``url`` is valid after ``start``."""

    def __init__(self, extension: Extension) -> None:
        self.extension = extension
        self.url = ""
        self._path = f"/cdp/{secrets.token_urlsafe(24)}"
        self._server: asyncio.Server | None = None
        self._client: asyncio.StreamWriter | None = None
        self._tabs: dict[int, _Tab] = {}
        self._attaching: dict[int, asyncio.Task[_Tab]] = {}
        self._auto = False  # Playwright asked for the tabs
        self._sessions = 0
        self._tasks: set[asyncio.Task[Any]] = set()

    @classmethod
    async def start(cls, name: str, directory: Path | None = None) -> Relay:
        """Connect to the extension and listen for Playwright.

        Raises:
            ExtensionError: the extension is not reachable.
        """
        relay = cls(await Extension.connect(name, directory))
        relay._server = await asyncio.start_server(relay._accept, "127.0.0.1", 0)
        port = relay._server.sockets[0].getsockname()[1]
        relay.url = f"ws://127.0.0.1:{port}{relay._path}"
        relay._spawn(relay._events())
        return relay

    async def close(self) -> None:
        """Stop listening and leave the extension."""
        if self._client is not None:
            self._client.close()
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await self.extension.close()

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the WebSocket -------------------------------------------------------

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            headers = {k.lower(): v.strip() for k, _, v in (h.partition(":") for h in head[1:])}
            path = head[0].split(" ")[1] if " " in head[0] else ""
            # A web page can open a WebSocket to localhost; it always sends an Origin.
            if (
                not secrets.compare_digest(path.encode(), self._path.encode())
                or "origin" in headers
                or "sec-websocket-key" not in headers
                or self._client is not None
            ):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                writer.close()
                return
            accept = base64.b64encode(
                hashlib.sha1(headers["sec-websocket-key"].encode() + _GUID).digest()  # noqa: S324 (the RFC says SHA-1)
            )
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            self._client = writer
            await self._messages(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _messages(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parts: list[bytes] = []
        while True:
            opcode, final, data = await read_frame(reader)
            if opcode == _CLOSE:
                return
            if opcode == _PING:
                writer.write(server_frame(_PONG, data))
            elif opcode != _PONG:
                parts.append(data)
                if final:
                    # Tasks start in the order they are made, so commands are forwarded in
                    # order while their replies may come back in any order.
                    self._spawn(self._command(json.loads(b"".join(parts))))
                    parts = []

    def _send(self, message: dict[str, Any]) -> None:
        if self._client is not None and not self._client.is_closing():
            self._client.write(server_frame(_TEXT, json.dumps(message).encode()))

    # --- the browser ---------------------------------------------------------

    async def _command(self, message: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"id": message["id"]}
        session = message.get("sessionId")
        if session:
            reply["sessionId"] = session
        try:
            method, params = message["method"], message.get("params") or {}
            if session:
                reply["result"] = await self._tab_command(session, method, params)
            else:
                reply["result"] = await self._browser_command(method, params)
        except Exception as exc:  # Playwright waits until every command is answered
            reply["error"] = {"code": -32000, "message": str(exc)}
        self._send(reply)

    async def _tab_command(self, session: str, method: str, params: dict[str, Any]) -> Any:
        for tab in self._tabs.values():
            child = session if session in tab.children else None
            if session == tab.session_id or child:
                result = await self.extension.call(
                    "cdp.send", tabId=tab.tab_id, sessionId=child, method=method, params=params
                )
                return result or {}
        raise ExtensionError(f"no tab for the session {session}")

    async def _browser_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "Browser.getVersion":
            info = await self.extension.call("info")
            return {
                "protocolVersion": "1.3",
                "product": f"Chrome/{info['version']}",
                "revision": "",
                "userAgent": info["userAgent"],
                "jsVersion": "",
            }
        if method == "Target.setAutoAttach":
            self._auto = True
            for tab_id in await self.extension.call("tabs.list"):
                with suppress(ExtensionError):  # a tab Chrome cannot debug, such as chrome://
                    await self._attach(tab_id)
            return {}
        if method == "Target.getTargetInfo":
            return {}
        if method == "Target.createTarget":
            tab_id = await self.extension.call("tabs.create", url=params.get("url"))
            tab = await self._attach(tab_id)
            return {"targetId": tab.target["targetId"]}
        if method in ("Target.closeTarget", "Target.activateTarget"):
            for tab in self._tabs.values():
                if tab.target["targetId"] == params.get("targetId"):
                    verb = "close" if method == "Target.closeTarget" else "activate"
                    await self.extension.call(f"tabs.{verb}", tabId=tab.tab_id)
                    return {"success": True}
            return {"success": False}
        if method in _IGNORED:
            return {}
        raise ExtensionError(f"{method} is not available in your Chrome through the extension")

    async def _attach(self, tab_id: int) -> _Tab:
        if tab := self._tabs.get(tab_id):
            return tab
        task = self._attaching.get(tab_id)
        if task is None:
            task = self._attaching[tab_id] = asyncio.create_task(self._attach_new(tab_id))
            task.add_done_callback(lambda _: self._attaching.pop(tab_id, None))
        return await task

    async def _attach_new(self, tab_id: int) -> _Tab:
        target = (await self.extension.call("cdp.attach", tabId=tab_id))["targetInfo"]
        self._sessions += 1
        tab = self._tabs[tab_id] = _Tab(tab_id, f"cabildo-{self._sessions}", target)
        self._send(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": tab.session_id,
                    "targetInfo": {**target, "attached": True},
                    "waitingForDebugger": False,
                },
            }
        )
        return tab

    async def _attach_quietly(self, tab_id: int) -> None:
        with suppress(ExtensionError):
            await self._attach(tab_id)

    def _detach(self, tab_id: int) -> None:
        if tab := self._tabs.pop(tab_id, None):
            self._send(
                {
                    "method": "Target.detachedFromTarget",
                    "params": {"sessionId": tab.session_id, "targetId": tab.target["targetId"]},
                }
            )

    async def _events(self) -> None:
        while (event := await self.extension.events.get()) is not None:
            method, params = event
            if method == "tab.added" and self._auto:
                self._spawn(self._attach_quietly(params["tabId"]))
            elif method == "tab.removed":
                self._detach(params["tabId"])
            elif method == "cdp.event" and (tab := self._tabs.get(params["tabId"])):
                name, inner = params["method"], params.get("params") or {}
                if name == "Target.attachedToTarget":
                    tab.children.add(inner["sessionId"])
                elif name == "Target.detachedFromTarget":
                    tab.children.discard(inner["sessionId"])
                self._send(
                    {
                        "sessionId": params.get("sessionId") or tab.session_id,
                        "method": name,
                        "params": inner,
                    }
                )
        if self._client is not None:  # the extension or Chrome went away
            self._client.close()
