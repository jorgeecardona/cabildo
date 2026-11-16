"""Chat client for an OpenAI-compatible endpoint (stdlib only)."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping, Sequence

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
        try:
            args = json.loads(fn["arguments"])
            if not isinstance(args, dict):
                args = {"__invalid__": fn["arguments"]}
        except json.JSONDecodeError:
            args = {"__invalid__": fn["arguments"]}
        calls.append(ToolCall(id=raw["id"], name=fn["name"], args=args))
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
