"""Shared plumbing for the hook scripts.

Hooks are dispatched by `cabildo-hook <event>` (see cli.py). They read the
hook JSON from stdin and emit additionalContext via stdout JSON. They stay
dependency-free (stdlib + cabildo.store/context) and never raise: a broken
hook must not break the user's session, so everything is best-effort.
"""

from __future__ import annotations

import json
import sys


def read_input() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def emit_context(event_name: str, text: str | None) -> None:
    """Emit additionalContext for an injecting hook (UserPromptSubmit /
    SessionStart / PostToolUse). Silence (no output) when text is falsy."""
    if not text:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }))
