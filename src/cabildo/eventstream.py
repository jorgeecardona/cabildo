"""Decode the AWS event stream of Bedrock's streaming Invoke API into server-sent events.

``InvokeModelWithResponseStream`` answers with ``application/vnd.amazon.eventstream``: binary
frames, each with headers and a payload. For Anthropic models a ``chunk`` frame holds
``{"bytes": "<base64 of one Anthropic event as JSON>"}``. Claude Code reads server-sent events, so
the gateway turns each frame into ``event: <type>`` and ``data: <json>`` lines.

A frame is: total length (4 bytes), headers length (4), CRC of those 8 bytes (4), the headers, the
payload, and a CRC of everything before it (4). All numbers are big-endian.
"""

from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Mapping
from dataclasses import dataclass

_PRELUDE = 12
_TRAILER = 4
_MAX_FRAME = 16 * 1024 * 1024
_STRING = 7
# Header value types that carry no length prefix: type -> number of bytes.
_FIXED = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
_BYTES = 6
_EXCEPTIONS = {
    "throttlingException": "rate_limit_error",
    "validationException": "invalid_request_error",
    "accessDeniedException": "permission_error",
    "resourceNotFoundException": "not_found_error",
    "serviceUnavailableException": "overloaded_error",
}
_STATUS_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}


class EventStreamError(ValueError):
    """The bytes are not a valid event stream."""


@dataclass(frozen=True)
class Frame:
    """One event of the stream: its string headers and its payload."""

    headers: Mapping[str, str]
    payload: bytes


def _headers(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    try:
        while i < len(raw):
            name_len = raw[i]
            name = raw[i + 1 : i + 1 + name_len].decode()
            kind = raw[i + 1 + name_len]
            i += 2 + name_len
            if kind in (_STRING, _BYTES):
                size = int.from_bytes(raw[i : i + 2], "big")
                value = raw[i + 2 : i + 2 + size]
                i += 2 + size
                if kind == _STRING:
                    out[name] = value.decode()
            elif kind in _FIXED:
                i += _FIXED[kind]
            else:
                msg = f"unknown header type {kind}"
                raise EventStreamError(msg)
    except (IndexError, UnicodeDecodeError) as exc:
        msg = "malformed frame headers"
        raise EventStreamError(msg) from exc
    return out


class Decoder:
    """Turn the bytes of an event stream into frames, as they arrive."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """The complete frames in what has arrived so far.

        Raises:
            EventStreamError: on a length or checksum that does not match.
        """
        self._buf += data
        frames: list[Frame] = []
        while len(self._buf) >= _PRELUDE:
            total = int.from_bytes(self._buf[0:4], "big")
            head = int.from_bytes(self._buf[4:8], "big")
            if zlib.crc32(bytes(self._buf[0:8])) != int.from_bytes(self._buf[8:12], "big"):
                msg = "bad prelude checksum"
                raise EventStreamError(msg)
            if not _PRELUDE + head + _TRAILER <= total <= _MAX_FRAME:
                msg = "bad frame length"
                raise EventStreamError(msg)
            if len(self._buf) < total:
                break
            body = bytes(self._buf[: total - _TRAILER])
            if zlib.crc32(body) != int.from_bytes(self._buf[total - _TRAILER : total], "big"):
                msg = "bad message checksum"
                raise EventStreamError(msg)
            frames.append(
                Frame(_headers(body[_PRELUDE : _PRELUDE + head]), body[_PRELUDE + head :])
            )
            del self._buf[:total]
        return frames


def _sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


def error_event(kind: str, message: str) -> bytes:
    """One SSE ``error`` event in the shape the Anthropic API uses."""
    body = json.dumps({"type": "error", "error": {"type": kind, "message": message}})
    return _sse("error", body)


def to_sse(frame: Frame) -> bytes:
    """The SSE text for ``frame``: an Anthropic event, an error event, or nothing."""
    kind = frame.headers.get(":message-type", "event")
    try:
        data = json.loads(frame.payload or b"{}")
    except ValueError:
        data = {}
    if kind == "exception":
        name = frame.headers.get(":exception-type", "")
        message = str(data.get("message", name)) if isinstance(data, dict) else name
        return error_event(_EXCEPTIONS.get(name, "api_error"), message)
    if kind == "error":
        return error_event("api_error", frame.headers.get(":error-message", "stream error"))
    if not isinstance(data, dict) or "bytes" not in data:
        return b""
    try:
        text = base64.b64decode(data["bytes"]).decode()
        event = json.loads(text)
    except (ValueError, TypeError) as exc:
        msg = "a chunk is not base64 JSON"
        raise EventStreamError(msg) from exc
    name = event.get("type", "message") if isinstance(event, dict) else "message"
    return _sse(str(name), text)


def error_body(status: int, body: bytes) -> bytes:
    """Bedrock's ``{"message": ...}`` error as an Anthropic ``{"type": "error", ...}`` body.

    A body that already has the Anthropic shape is returned as it is.
    """
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("type") == "error":
        return body
    if isinstance(data, dict) and "message" in data:
        message = str(data["message"])
    else:
        message = body[:300].decode("utf-8", "replace") or f"HTTP {status}"
    kind = _STATUS_TYPES.get(status, "api_error")
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
