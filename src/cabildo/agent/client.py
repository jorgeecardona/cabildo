"""Chat client for an OpenAI-compatible endpoint (stdlib only)."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence

from .types import Endpoint, Message, Tool, ToolCall


def _wire_message(m: Message) -> dict[str, object]:
    out: dict[str, object] = {"role": m.role, "content": m.text}
    if m.calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(dict(c.args))},
            }
            for c in m.calls
        ]
    if m.call_id:
        out["tool_call_id"] = m.call_id
    return out


def _wire_tool(t: Tool) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": dict(t.parameters),
        },
    }


def _parse_args(raw: str) -> Mapping[str, object]:
    """Decode a tool call's JSON arguments, or mark them invalid for the loop to report."""
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        return {"__invalid__": raw}
    return args if isinstance(args, dict) else {"__invalid__": raw}


def parse_reply(body: Mapping[str, object]) -> Message:
    """Turn a chat-completions response body into an assistant ``Message``.

    A tool call whose ``arguments`` is not valid JSON becomes a call with an
    ``{"__invalid__": raw}`` argument, so the loop can send the error back to the model.
    """
    choices = body["choices"]
    assert isinstance(choices, list)  # noqa: S101
    msg = choices[0]["message"]
    calls: list[ToolCall] = []
    for raw in msg.get("tool_calls") or []:
        fn = raw["function"]
        calls.append(ToolCall(id=raw["id"], name=fn["name"], args=_parse_args(fn["arguments"])))
    return Message(role="assistant", text=msg.get("content") or "", calls=tuple(calls))


def chat(endpoint: Endpoint, messages: Sequence[Message], tools: Sequence[Tool]) -> Message:
    """Send ``messages`` and return the model's reply."""
    payload = {
        "model": endpoint.model,
        "messages": [_wire_message(m) for m in messages],
        "tools": [_wire_tool(t) for t in tools],
    }
    req = urllib.request.Request(
        f"{endpoint.base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=endpoint.timeout_s) as resp:
        return parse_reply(json.load(resp))


def assemble_stream(lines: Iterable[str], on_text: Callable[[str], None]) -> Message:
    """Build an assistant ``Message`` from server-sent-event lines of a streamed reply.

    ``on_text`` gets each piece of text as it arrives. Tool call arguments arrive in
    pieces and are joined by ``index``.
    """
    text: list[str] = []
    slots: dict[int, dict[str, str]] = {}
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            break
        choices = json.loads(data).get("choices") or []
        delta = (choices[0].get("delta") if choices else None) or {}
        if piece := delta.get("content"):
            text.append(piece)
            on_text(piece)
        for tc in delta.get("tool_calls") or []:
            slot = slots.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
            fn = tc.get("function") or {}
            slot["id"] = tc.get("id") or slot["id"]
            slot["name"] = fn.get("name") or slot["name"]
            slot["args"] += fn.get("arguments") or ""
    calls = tuple(
        ToolCall(id=v["id"] or f"call_{i}", name=v["name"], args=_parse_args(v["args"] or "{}"))
        for i, v in sorted(slots.items())
    )
    return Message(role="assistant", text="".join(text), calls=calls)


def chat_stream(
    endpoint: Endpoint,
    messages: Sequence[Message],
    tools: Sequence[Tool],
    *,
    on_text: Callable[[str], None],
) -> Message:
    """Like ``chat``, but streams: ``on_text`` gets the text as the model writes it."""
    payload = {
        "model": endpoint.model,
        "messages": [_wire_message(m) for m in messages],
        "tools": [_wire_tool(t) for t in tools],
        "stream": True,
    }
    req = urllib.request.Request(
        f"{endpoint.base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=endpoint.timeout_s) as resp:
        return assemble_stream((raw.decode() for raw in resp), on_text)
