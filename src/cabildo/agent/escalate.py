"""The ``escalate`` tool: ask a stronger Claude model a hard question.

Off unless ``cabildo code --escalate`` is given. Each call is billed to the Anthropic API
key in ``ANTHROPIC_API_KEY``, so the number of calls per task is capped.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from .types import Context, Tool

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-fable-5-2"
MAX_TOKENS = 2048

Ask = Callable[[str], str]


def parse_text(body: Mapping[str, object]) -> str:
    """Join the text blocks of a Messages API response body."""
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text")


def anthropic_ask(model: str, api_key: str, *, timeout_s: float = 120.0) -> Ask:
    """Return a function that sends one question to ``model`` and returns the answer text."""

    def ask(question: str) -> str:
        payload = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": question}],
        }
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return parse_text(json.load(resp))

    return ask


class Escalator:
    """Counts escalations and refuses once ``limit`` is reached."""

    def __init__(self, ask: Ask, *, limit: int) -> None:
        self._ask = ask
        self._limit = limit
        self._used = 0

    def run(self, args: Mapping[str, object], _ctx: Context) -> str:
        """Tool body: forward ``question`` to the stronger model."""
        if self._used >= self._limit:
            return f"error: escalation limit reached ({self._limit}). Continue on your own."
        self._used += 1
        try:
            return self._ask(str(args["question"]))
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            return f"error: escalation failed: {exc}"

    def tool(self) -> Tool:
        """The tool definition to add to the agent's tool list."""
        return Tool(
            name="escalate",
            description=(
                "Ask a stronger model a hard question: a design choice, a plan, or a bug you "
                "cannot fix. It costs money, so use it rarely. It sees only your question, so "
                "paste the code it needs."
            ),
            parameters={
                "type": "object",
                "properties": {"question": {"type": "string", "description": "The question."}},
                "required": ["question"],
            },
            run=self.run,
        )
