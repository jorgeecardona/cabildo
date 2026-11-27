"""The program Chrome starts for the cabildo extension (``cabildo browser extension host``).

It passes messages between the extension and one browser server at a time:

- to Chrome over stdin and stdout, in Chrome's native messaging format (a 4-byte length, then
  JSON);
- to the browser server over a Unix socket that only this user can open, one JSON object per
  line. The socket is ``<cabildo home>/chrome/host-<pid>.sock``.

It opens no network port. It ends when Chrome closes its stdin.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from cabildo import store

HOST_NAME = "dev.cabildo.chrome"
# Chrome refuses a message from a native host over 1 MB. A larger one is sent in parts of this
# many characters: even when every character needs a 6-byte escape, a part stays under 1 MB.
_PART_CHARS = 150_000
_LIMIT_BYTES = 900_000

Message = dict[str, Any]


def socket_dir() -> Path:
    """Where the sockets of the running hosts are."""
    return store.home() / "chrome"


def read_native(stream: BinaryIO) -> Iterator[Message]:
    """The messages Chrome writes to ``stream``, until it closes it."""
    while len(head := stream.read(4)) == 4:
        (length,) = struct.unpack("=I", head)
        body = stream.read(length)
        if len(body) < length:
            return
        yield json.loads(body)


def native_frames(message: Message) -> Iterator[bytes]:
    """``message`` as the frames to write to Chrome: one, or its parts when it is too large."""
    text = json.dumps(message)
    bodies = [text.encode()]
    if len(bodies[0]) > _LIMIT_BYTES:
        parts = [text[i : i + _PART_CHARS] for i in range(0, len(text), _PART_CHARS)]
        bodies = [
            json.dumps({"type": "part", "index": i, "count": len(parts), "data": part}).encode()
            for i, part in enumerate(parts)
        ]
    for body in bodies:
        yield struct.pack("=I", len(body)) + body


@dataclass
class _Client:
    conn: socket.socket
    name: str


class Host:
    """Messages between Chrome and the one connected browser server."""

    def __init__(self, to_chrome: BinaryIO) -> None:
        self._out = to_chrome
        self._lock = threading.Lock()  # guards _client, _calls, _next and both writers
        self._client: _Client | None = None
        self._calls: dict[int, int] = {}  # our call id -> the client's
        self._next = 0

    def _to_chrome(self, message: Message) -> None:
        for frame in native_frames(message):
            self._out.write(frame)
        self._out.flush()

    def _to_client(self, message: Message) -> None:
        if self._client is not None:
            with suppress(OSError):
                self._client.conn.sendall(json.dumps(message).encode() + b"\n")

    def from_chrome(self, message: Message) -> None:
        """Pass a result or an event to the client. A result of an earlier client is dropped."""
        with self._lock:
            if message.get("type") == "event":
                self._to_client(message)
            elif message.get("type") in ("result", "error"):
                theirs = self._calls.pop(message.get("id", -1), None)
                if theirs is not None:
                    self._to_client({**message, "id": theirs})

    def serve_client(self, conn: socket.socket) -> None:
        """Serve one connection until it closes. A second one at the same time is refused."""
        with conn, conn.makefile("rb") as lines:
            try:
                hello = json.loads(lines.readline() or b"null")
                name = str(hello["name"])
            except (ValueError, KeyError, TypeError, OSError):
                return
            with self._lock:
                if self._client is not None:
                    reason = f"the browser is in use by {self._client.name}"
                    with suppress(OSError):
                        refusal = {"type": "refused", "reason": reason}
                        conn.sendall(json.dumps(refusal).encode() + b"\n")
                    return
                self._client = _Client(conn, name)
                self._to_client({"type": "welcome"})
                self._to_chrome({"type": "client", "name": name})
            try:
                for line in lines:
                    call = json.loads(line)
                    with self._lock:
                        self._next += 1
                        self._calls[self._next] = call["id"]
                        self._to_chrome({**call, "type": "call", "id": self._next})
            except (ValueError, KeyError, TypeError, OSError):
                pass
            finally:
                with self._lock:
                    self._client = None
                    self._calls.clear()
                    with suppress(OSError):
                        self._to_chrome({"type": "client", "name": None})


def listen(path: Path) -> socket.socket:
    """A Unix socket at ``path`` that only this user can open."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    server.listen()
    return server


def run(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Serve Chrome on ``stdin`` and ``stdout`` until it closes them."""
    source: BinaryIO = sys.stdin.buffer if stdin is None else stdin
    sink: BinaryIO = sys.stdout.buffer if stdout is None else stdout
    host = Host(sink)
    path = socket_dir() / f"host-{os.getpid()}.sock"
    server = listen(path)

    def accept() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=host.serve_client, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        for message in read_native(source):
            host.from_chrome(message)
    finally:
        server.close()
        path.unlink(missing_ok=True)
    return 0
