"""wsclient: the handshake and frames, against a small server written here."""

from __future__ import annotations

import socket

import pytest

from cabildo.wsclient import WebSocket, WebSocketError
from tests.wsserver import frame, read_frame


def test_connect_sends_the_headers_and_text_goes_both_ways(serve) -> None:
    seen: dict[str, str] = {}

    def echo(conn: socket.socket, headers: dict[str, str]) -> None:
        seen.update(headers)
        op, data = read_frame(conn)
        conn.sendall(frame(op, b"echo:" + data))

    ws = WebSocket.connect(serve(echo), headers={"Authorization": "token abc"})
    ws.send("héllo")
    assert ws.recv() == "echo:héllo"
    ws.close()
    assert seen["authorization"] == "token abc"


def test_a_long_message_and_a_split_message_arrive_whole(serve) -> None:
    big = "x" * 70_000

    def server(conn: socket.socket, headers: dict[str, str]) -> None:
        _, data = read_frame(conn)
        conn.sendall(frame(0x1, data))
        conn.sendall(frame(0x1, b"one ", final=False) + frame(0x0, b"two"))

    ws = WebSocket.connect(serve(server))
    ws.send(big)
    assert ws.recv() == big
    assert ws.recv() == "one two"


def test_a_ping_is_answered_and_skipped(serve) -> None:
    got: list[tuple[int, bytes]] = []

    def server(conn: socket.socket, headers: dict[str, str]) -> None:
        conn.sendall(frame(0x9, b"hi") + frame(0x1, b"after"))
        got.append(read_frame(conn))

    ws = WebSocket.connect(serve(server))
    assert ws.recv() == "after"
    ws.close()
    assert got == [(0xA, b"hi")]


def test_close_and_refusal_raise(serve) -> None:
    def closer(conn: socket.socket, headers: dict[str, str]) -> None:
        conn.sendall(frame(0x8, b""))

    ws = WebSocket.connect(serve(closer))
    with pytest.raises(WebSocketError, match="closed"):
        ws.recv()
    with pytest.raises(WebSocketError, match="403"):
        WebSocket.connect(serve(closer, False))
    with pytest.raises(WebSocketError, match="only ws://"):
        WebSocket.connect("wss://example.com/x")


def test_recv_times_out(serve) -> None:
    def silent(conn: socket.socket, headers: dict[str, str]) -> None:
        conn.recv(10)

    ws = WebSocket.connect(serve(silent))
    ws.settimeout(0.2)
    with pytest.raises(TimeoutError):
        ws.recv()
