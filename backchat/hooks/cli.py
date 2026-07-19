"""Console entry point for the hook scripts: `backchat-hook <event>`.

Claude Code invokes this once per hook event with the event JSON on stdin.
Dispatching through an installed console script (instead of a file path into
the repo) means settings.json survives repo moves/renames — the command only
depends on the venv the package is installed in.

A broken hook must never break the user's session: everything, including the
import of the event module itself, runs inside the catch-all.
"""

from __future__ import annotations

import importlib
import sys

EVENTS = (
    "session_start",
    "prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "stop",
    "session_end",
)


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in EVENTS:
        print(f"backchat-hook: unknown event {name!r} "
              f"(choose from: {', '.join(EVENTS)})", file=sys.stderr)
        return
    try:
        importlib.import_module(f"backchat.hooks.{name}").main()
    except Exception as e:
        print(f"[backchat {name} hook error: {e}]", file=sys.stderr)


if __name__ == "__main__":
    main()
