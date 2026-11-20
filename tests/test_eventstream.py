"""The AWS event stream of Bedrock becomes the server-sent events Claude Code reads."""

from __future__ import annotations

import base64
import json
import zlib

import pytest

from cabildo import eventstream


def frame(headers: dict[str, str], payload: bytes) -> bytes:
    """One event stream frame, built the way AWS builds it."""
    head = b"".join(
        bytes([len(k)]) + k.encode() + b"\x07" + len(v).to_bytes(2, "big") + v.encode()
        for k, v in headers.items()
    )
    total = 12 + len(head) + len(payload) + 4
    prelude = total.to_bytes(4, "big") + len(head).to_bytes(4, "big")
    body = prelude + zlib.crc32(prelude).to_bytes(4, "big") + head + payload
    return body + zlib.crc32(body).to_bytes(4, "big")


def chunk(event: dict) -> bytes:
    """A ``chunk`` frame that carries one Anthropic event."""
    inner = base64.b64encode(json.dumps(event).encode()).decode()
    return frame(
        {":message-type": "event", ":event-type": "chunk"}, json.dumps({"bytes": inner}).encode()
    )


def test_a_chunk_becomes_one_sse_event_named_by_its_type() -> None:
    event = {"type": "content_block_delta", "delta": {"text": "ok"}}
    (f,) = eventstream.Decoder().feed(chunk(event))
    assert eventstream.to_sse(f) == (
        b"event: content_block_delta\ndata: " + json.dumps(event).encode() + b"\n\n"
    )


def test_frames_split_at_any_byte_arrive_whole() -> None:
    raw = chunk({"type": "message_start"}) + chunk({"type": "message_stop"})
    decoder = eventstream.Decoder()
    got = []
    for i in range(0, len(raw), 7):
        got += decoder.feed(raw[i : i + 7])
    assert [json.loads(base64.b64decode(json.loads(g.payload)["bytes"]))["type"] for g in got] == [
        "message_start",
        "message_stop",
    ]


def test_an_exception_frame_becomes_an_error_event() -> None:
    raw = frame(
        {":message-type": "exception", ":exception-type": "throttlingException"},
        b'{"message": "slow down"}',
    )
    (f,) = eventstream.Decoder().feed(raw)
    out = eventstream.to_sse(f)
    assert out.startswith(b"event: error\n")
    assert json.loads(out.split(b"data: ")[1])["error"] == {
        "type": "rate_limit_error",
        "message": "slow down",
    }


def test_headers_of_other_types_are_skipped() -> None:
    head = bytes([1]) + b"n" + b"\x04" + (7).to_bytes(4, "big")  # an int32 header
    head += bytes([13]) + b":message-type" + b"\x07" + (5).to_bytes(2, "big") + b"event"
    payload = b"{}"
    total = 12 + len(head) + len(payload) + 4
    prelude = total.to_bytes(4, "big") + len(head).to_bytes(4, "big")
    body = prelude + zlib.crc32(prelude).to_bytes(4, "big") + head + payload
    raw = body + zlib.crc32(body).to_bytes(4, "big")
    (f,) = eventstream.Decoder().feed(raw)
    assert f.headers == {":message-type": "event"}


@pytest.mark.parametrize("where", [4, 9, -2])
def test_a_flipped_byte_is_refused(where: int) -> None:
    raw = bytearray(chunk({"type": "message_start"}))
    raw[where] ^= 0xFF
    with pytest.raises(eventstream.EventStreamError):
        eventstream.Decoder().feed(bytes(raw))


def test_a_chunk_that_is_not_base64_json_is_refused() -> None:
    raw = frame({":message-type": "event"}, b'{"bytes": "!!!"}')
    (f,) = eventstream.Decoder().feed(raw)
    with pytest.raises(eventstream.EventStreamError):
        eventstream.to_sse(f)


def test_error_body_wraps_a_bedrock_message_and_keeps_an_anthropic_error() -> None:
    wrapped = json.loads(eventstream.error_body(429, b'{"message": "Too many requests"}'))
    assert wrapped == {
        "type": "error",
        "error": {"type": "rate_limit_error", "message": "Too many requests"},
    }
    ours = b'{"type": "error", "error": {"type": "x", "message": "y"}}'
    assert eventstream.error_body(400, ours) == ours
    assert json.loads(eventstream.error_body(500, b"boom"))["error"]["message"] == "boom"
