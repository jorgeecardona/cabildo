"""A small WebSocket server for tests: ``serve(handler)`` returns a ws:// url."""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
from collections.abc import Callable, Iterator

import pytest

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def read_frame(conn: socket.socket) -> tuple[int, bytes]:
    """One client frame: (opcode, unmasked payload)."""

    def take(n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = conn.recv(n - len(out))
            if not chunk:
                raise ConnectionError
            out += chunk
        return out

    b0, b1 = take(2)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack(">H", take(2))
    elif n == 127:
        (n,) = struct.unpack(">Q", take(8))
    mask = take(4)
    data = take(n)
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def frame(opcode: int, payload: bytes, final: bool = True) -> bytes:
    """A server frame (never masked)."""
    head = bytes([(0x80 if final else 0) | opcode])
    n = len(payload)
    if n < 126:
        return head + bytes([n]) + payload
    if n < 1 << 16:
        return head + bytes([126]) + struct.pack(">H", n) + payload
    return head + bytes([127]) + struct.pack(">Q", n) + payload


Handler = Callable[[socket.socket, dict[str, str]], None]


@pytest.fixture
def serve() -> Iterator[Callable[[Handler, bool], str]]:
    """``serve(handler)`` starts a one-connection server and returns its ws:// url."""
    socks: list[socket.socket] = []

    def start(handler: Handler, accept: bool = True) -> str:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        socks.append(srv)

        def run() -> None:
            conn, _ = srv.accept()
            with conn:
                raw = b""
                while b"\r\n\r\n" not in raw:
                    raw += conn.recv(4096)
                lines = raw.decode().split("\r\n")
                headers = {k.lower(): v for k, _, v in (ln.partition(": ") for ln in lines[1:])}
                if not accept:
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                    return
                key = headers["sec-websocket-key"].encode()
                accept_key = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()  # noqa: S324
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                    + accept_key.encode()
                    + b"\r\n\r\n"
                )
                handler(conn, headers)

        threading.Thread(target=run, daemon=True).start()
        return f"ws://127.0.0.1:{srv.getsockname()[1]}/chan?x=1"

    yield start
    for s in socks:
        s.close()
