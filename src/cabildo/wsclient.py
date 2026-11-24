"""A minimal WebSocket client (RFC 6455) for ``ws://`` on the local machine.

It does what the Jupyter Server channel needs and nothing more: a client handshake with extra
headers, text frames in both directions, ping and pong, close. No extensions, no ``wss://``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import struct
from urllib.parse import urlsplit

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, CLOSE, PING, PONG = 0x1, 0x8, 0x9, 0xA


class WebSocketError(OSError):
    """The handshake failed or the peer broke the protocol."""


class WebSocket:
    """One open connection. Use ``connect``; ``send`` and ``recv`` handle whole text messages."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(
        cls, url: str, headers: dict[str, str] | None = None, timeout: float = 10.0
    ) -> WebSocket:
        """Open ``ws://host:port/path`` and finish the handshake.

        Raises:
            WebSocketError: the server did not answer ``101 Switching Protocols``.
        """
        parts = urlsplit(url)
        if parts.scheme != "ws" or not parts.hostname:
            raise WebSocketError(f"only ws:// urls work, got {url!r}")
        sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16))
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {parts.hostname}:{parts.port or 80}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key.decode()}",
            "Sec-WebSocket-Version: 13",
            *(f"{k}: {v}" for k, v in (headers or {}).items()),
        ]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        ws = cls(sock)
        head = ws._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            sock.close()
            raise WebSocketError(f"handshake refused: {status.decode(errors='replace')}")
        want = base64.b64encode(hashlib.sha1(key + _GUID).digest())  # noqa: S324 (the RFC says SHA-1)
        if want.lower() not in head.lower():
            sock.close()
            raise WebSocketError("handshake: wrong Sec-WebSocket-Accept")
        return ws

    def _read_until(self, end: bytes) -> bytes:
        while end not in self._buf:
            self._buf += self._chunk()
        head, _, self._buf = self._buf.partition(end)
        return head

    def _chunk(self) -> bytes:
        data = self._sock.recv(65536)
        if not data:
            raise WebSocketError("connection closed")
        return data

    def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += self._chunk()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def settimeout(self, timeout: float | None) -> None:
        """Seconds ``recv`` waits for the next byte; None waits forever."""
        self._sock.settimeout(timeout)

    def _frame(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)  # a client must mask what it sends
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(head + mask + masked)

    def send(self, text: str) -> None:
        """Send one text message."""
        self._frame(TEXT, text.encode())

    def recv(self) -> str:
        """The next text message. Answers pings; raises ``WebSocketError`` on close.

        Raises:
            TimeoutError: nothing arrived within the socket timeout (``settimeout``).
        """
        message = b""
        while True:
            b0, b1 = self._take(2)
            opcode, final = b0 & 0x0F, bool(b0 & 0x80)
            n = b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack(">H", self._take(2))
            elif n == 127:
                (n,) = struct.unpack(">Q", self._take(8))
            payload = self._take(n)  # a server never masks
            if opcode == PING:
                self._frame(PONG, payload)
            elif opcode == CLOSE:
                raise WebSocketError("closed by the server")
            elif opcode in (TEXT, 0x0, 0x2):
                message += payload
                if final:
                    return message.decode()

    def close(self) -> None:
        """Say goodbye and drop the connection; never raises."""
        try:
            self._frame(CLOSE, b"")
        except OSError:
            pass
        self._sock.close()
