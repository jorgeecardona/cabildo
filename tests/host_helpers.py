"""Helpers the host tests share: a raw viewer on the host socket, and reading its output."""

from __future__ import annotations

import json
import socket

from cabildo import host, host_client


def viewer(path: str, host_id: str, rows: int = 24, cols: int = 80) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(15)  # a loaded machine needs more than 5 s
    sock.connect(path)
    sock.sendall(
        json.dumps({"op": "attach", "id": host_id, "rows": rows, "cols": cols}).encode() + b"\n"
    )
    assert host_client._read_line(sock)["ok"]
    return sock


def read_until(sock: socket.socket, needle: bytes) -> tuple[bytes, int | None]:
    """Terminal bytes until ``needle`` shows, and the exit code if the session ended first."""
    seen = b""
    while needle not in seen:
        frame = host.read_frame(sock)
        if frame is None:
            return seen, None
        kind, payload = frame
        if kind == host.DATA:
            seen += payload
        elif kind == host.EXIT:
            return seen, host.unpack_code(payload)
    return seen, None


def type_in(sock: socket.socket, text: bytes) -> None:
    sock.sendall(host.pack_frame(host.DATA, text))
