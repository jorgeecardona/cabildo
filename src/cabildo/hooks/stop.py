#!/usr/bin/env python3
"""Stop hook: refuse to let an agent go idle while it has unread URGENT mail, or
when a heartbeat nudge is due.

The bus is pull-based — a message is only surfaced by the UserPromptSubmit hook,
i.e. on the *next* human turn. An agent that finishes and idles with urgent mail
in its inbox is therefore unreachable until a human types at it. This hook closes
that gap from the other side: instead of waking a stopped session (which Claude
Code has no mechanism for), it stops the session from stopping.

Two reasons block, urgent mail first:
  * unread `urgent=True` messages (ordinary chatter never holds a turn open);
  * an opt-in heartbeat (`[timeline].heartbeat_s` > 0) that has come due, to defer
    an imminent idle by one turn so a long-running agent re-reads the clock and
    picks its standing work back up.

Both are one-shot: the `stop_hook_active` guard below means a block buys exactly
one more turn, never a runaway loop. Neither can wake a session that has already
stopped — no hook can — they only defer a stop that is happening right now.
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
    cwd = data.get("cwd")

    from cabildo import config, notify, store

    conn = store.connect()
    try:
        # Urgent mail wins: it is a message someone is waiting on, and checking the
        # heartbeat first would spend this turn's single block on the clock instead.
        reason = notify.urgent_block_reason(conn, session_id)
        if not reason:
            reason = notify.heartbeat_reason(conn, session_id, config.timeline_heartbeat(cwd))
    finally:
        conn.close()

    if not reason:
        return
    print(json.dumps({"decision": "block", "reason": reason}))
