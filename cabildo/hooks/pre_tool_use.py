#!/usr/bin/env python3
"""PreToolUse hook: enforce claims at the tool-call boundary.

The claims table is advisory — agents honor it by convention. This hook is the
convention's teeth: a `git push` while another session holds push:<project>, or
an edit to a file another session holds file:<path> on, is denied with a reason
naming the holder. The denied agent sees the reason and can negotiate on the
bus instead of clobbering in-flight work. All decision logic lives in
cabildo.guard; this script is only plumbing.
"""

from __future__ import annotations

import json

from cabildo.hooks._common import read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    tool_name = data.get("tool_name")
    if not session_id or not tool_name:
        return

    from cabildo import guard, store

    conn = store.connect()
    try:
        reason = guard.deny_reason(conn, session_id, tool_name,
                                   data.get("tool_input") or {})
    finally:
        conn.close()

    if not reason:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))

