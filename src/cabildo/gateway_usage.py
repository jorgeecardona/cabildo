"""The tokens each gateway upstream served: read from its replies, kept in the bus DB.

The gateway feeds a ``Counter`` the bytes of a reply as it sends them, and records the counts
under the upstream's name. ``spent`` prices them at Anthropic's list prices (``prices``).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import astuple, dataclass
from typing import Any

from . import store, usage

_MAX_LINE = 1 << 20  # a stream line with usage is far shorter; a longer one is skipped
_MAX_BODY = 8 << 20  # the most of a reply that is not streamed that is kept to read its usage
_KEEP_S = 400 * 86400.0


@dataclass(frozen=True)
class Reply:
    """One reply an upstream served, in the column order of the ``gateway_usage`` table."""

    ts: float
    upstream: str
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    cache_write_1h: int
    fast: bool


@dataclass(frozen=True)
class Spent:
    """What one upstream served: today (since local midnight), 7 days, and all that is kept."""

    upstream: str
    today: usage.Window
    week: usage.Window
    all_time: usage.Window


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


class Counter:
    """Read the ``usage`` of one reply from its bytes.

    A streamed reply gives it in ``message_start`` and again, with the final output count, in
    ``message_delta``: a later value replaces an earlier one. Any other reply is one JSON body.
    """

    def __init__(self, *, streamed: bool) -> None:
        self._streamed = streamed
        self._buf = bytearray()
        self._usage: dict[str, Any] = {}

    def feed(self, data: bytes) -> None:
        """Take the next bytes of the reply."""
        if not self._streamed:
            if len(self._buf) <= _MAX_BODY:
                self._buf += data
            return
        self._buf += data
        *lines, rest = self._buf.split(b"\n")
        for line in lines:
            if line.startswith(b"data:") and b'"usage"' in line:
                self._take(bytes(line[5:]))
        self._buf = bytearray(rest if len(rest) <= _MAX_LINE else b"")

    def _take(self, text: bytes) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        message = data.get("message")
        found = data.get("usage") or (message.get("usage") if isinstance(message, dict) else None)
        if isinstance(found, Mapping):
            self._usage.update({k: v for k, v in found.items() if v is not None})

    def reply(self, upstream: str, model: str, at: float) -> Reply | None:
        """The counts of the reply; None when it reported no tokens."""
        if not self._streamed:
            self._take(bytes(self._buf))
        found = self._usage
        writes = found.get("cache_creation")
        hour = writes.get("ephemeral_1h_input_tokens") if isinstance(writes, Mapping) else 0
        counted = Reply(
            ts=at,
            upstream=upstream,
            model=model,
            input=_count(found.get("input_tokens")),
            output=_count(found.get("output_tokens")),
            cache_read=_count(found.get("cache_read_input_tokens")),
            cache_write=_count(found.get("cache_creation_input_tokens")),
            cache_write_1h=_count(hour),
            fast=found.get("speed") == "fast",
        )
        used = counted.input + counted.output + counted.cache_read + counted.cache_write
        return counted if used else None


def add(conn: sqlite3.Connection, reply: Reply) -> None:
    """Keep ``reply``, and drop what is older than 400 days."""
    with conn:
        conn.execute("INSERT INTO gateway_usage VALUES (?,?,?,?,?,?,?,?,?)", astuple(reply))
        conn.execute("DELETE FROM gateway_usage WHERE ts < ?", (reply.ts - _KEEP_S,))


def recorder(log: Callable[[str], None]) -> Callable[[Reply], None]:
    """A function that writes a reply to the bus DB. A failure is logged, never raised."""

    def record(reply: Reply) -> None:
        try:
            with contextlib.closing(store.connect()) as conn:
                add(conn, reply)
        except (sqlite3.Error, OSError) as exc:
            log(f"gateway: tokens of {reply.upstream} not recorded: {exc}")

    return record


def spent(names: tuple[str, ...], now: float | None = None) -> tuple[Spent, ...]:
    """The upstreams of ``names`` that served something, in that order. Never raises."""
    at = time.time() if now is None else now
    if not store.db_path().exists():
        return ()
    try:
        with contextlib.closing(store.connect()) as conn:
            found = tuple(
                Spent(
                    name,
                    today=usage.upstream_window(conn, usage.midnight(at), name),
                    week=usage.upstream_window(conn, at - 7 * usage.DAY, name),
                    all_time=usage.upstream_window(conn, 0.0, name),
                )
                for name in names
            )
    except (sqlite3.Error, OSError):
        return ()
    return tuple(s for s in found if s.all_time.tokens)
