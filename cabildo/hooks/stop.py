#!/usr/bin/env python3
"""Stop hook: refuse to let an agent go idle while it has unread URGENT mail.

The bus is pull-based — a message is only surfaced by the UserPromptSubmit hook,
i.e. on the *next* human turn. An agent that finishes and idles with urgent mail
in its inbox is therefore unreachable until a human types at it. This hook closes
that gap from the other side: instead of waking a stopped session (which Claude
Code has no mechanism for), it stops the session from stopping.

Only `urgent=True` messages block; ordinary chatter must never hold a turn open.
"""

from __future__ import annotations

import json

from cabildo.hooks._common import read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return
    # Claude Code sets this when the turn is already continuing because we
    # blocked it. Blocking again here is how you build an infinite loop.
    if data.get("stop_hook_active"):
        return

    from cabildo import store
    from cabildo import notify

    conn = store.connect()
    try:
        reason = notify.urgent_block_reason(conn, session_id)
    finally:
        conn.close()

    if not reason:
        return
    print(json.dumps({"decision": "block", "reason": reason}))

