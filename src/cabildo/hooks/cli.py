"""Console entry point for the hook scripts: `cabildo-hook <event>`.

Claude Code invokes this once per hook event with the event JSON on stdin.
Dispatching through an installed console script (instead of a file path into
the repo) means settings.json survives repo moves/renames — the command only
depends on the venv the package is installed in.

A broken hook must never break the user's session: everything, including the
import of the event module itself, runs inside the catch-all.

This dispatcher is also the single chokepoint every hook flows through, so it is
where cost telemetry is captured: it times the hook and measures the bytes of
context the hook pushed back, recording both to hook_metrics (best-effort). That
is what makes "how much does running cabildo cost me" measurable after the fact.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import time

EVENTS = (
    "session_start",
    "prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "stop",
    "subagent_stop",
    "session_end",
)


def _injected_len(output: str) -> int:
    """Chars of context this hook pushed into the model: additionalContext plus
    any deny/stop reason (all land in the model's context). 0 if unparseable.
    """
    try:
        obj = json.loads(output)
    except Exception:
        return 0
    if not isinstance(obj, dict):
        return 0
    total = 0
    hso = obj.get("hookSpecificOutput")
    if isinstance(hso, dict):
        for k in ("additionalContext", "permissionDecisionReason"):
            v = hso.get(k)
            if isinstance(v, str):
                total += len(v)
    for k in ("reason", "stopReason"):
        v = obj.get(k)
        if isinstance(v, str):
            total += len(v)
    return total


def _record(event: str, raw_input: str, output: str, elapsed_ms: float) -> None:
    """Best-effort cost-telemetry write. Never raises into the caller."""
    try:
        session_id = None
        try:
            session_id = (json.loads(raw_input or "{}") or {}).get("session_id")
        except Exception:
            pass
        from cabildo import store

        conn = store.connect()
        try:
            store.record_hook_metric(
                conn,
                session_id=session_id,
                event=event,
                injected_chars=_injected_len(output),
                elapsed_ms=elapsed_ms,
            )
        finally:
            conn.close()
    except Exception:
        pass


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in EVENTS:
        print(
            f"cabildo-hook: unknown event {name!r} (choose from: {', '.join(EVENTS)})",
            file=sys.stderr,
        )
        return

    # Read stdin once here so we can both measure it and still hand it to the
    # hook module (which reads sys.stdin itself); replay it via a StringIO.
    raw_input = sys.stdin.read()
    sys.stdin = io.StringIO(raw_input)

    # Capture the hook's stdout so we can measure the injected context, then
    # re-emit it verbatim — Claude Code parses this stdout, so it must pass
    # through unchanged.
    real_stdout = sys.stdout
    buf = io.StringIO()
    start = time.monotonic()
    try:
        sys.stdout = buf
        importlib.import_module(f"cabildo.hooks.{name}").main()
    except Exception as e:
        sys.stdout = real_stdout
        print(f"[cabildo {name} hook error: {e}]", file=sys.stderr)
        return
    finally:
        sys.stdout = real_stdout
    elapsed_ms = (time.monotonic() - start) * 1000.0

    output = buf.getvalue()
    if output:
        real_stdout.write(output)
        real_stdout.flush()
    _record(name, raw_input, output, elapsed_ms)


if __name__ == "__main__":
    main()
