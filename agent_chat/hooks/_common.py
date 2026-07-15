"""Shared plumbing for the hook scripts.

Hooks are launched by Claude Code as standalone commands. They must:
  * import the agent_chat package without it being pip-installed, so we inject
    the repo root onto sys.path;
  * read the hook JSON from stdin;
  * emit additionalContext via stdout JSON.
They stay dependency-free (stdlib + agent_chat.store/context) and never raise:
a broken hook must not break the user's session, so everything is best-effort.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# repo root = parents[2] of this file: hooks/ -> agent_chat/ -> <repo>
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def read_input() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def emit_context(event_name: str, text: str | None) -> None:
    """Emit additionalContext for an injecting hook (UserPromptSubmit /
    SessionStart). Silence (no output) when text is falsy."""
    if not text:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }))
