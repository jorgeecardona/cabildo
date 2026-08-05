#!/usr/bin/env python3
"""PostToolUse hook: mid-turn mail delivery. Fires after every tool call, which
is the only interrupt point Claude Code gives us inside a turn — an agent that
is thinking or writing text cannot be reached until its next tool call.

Three jobs, all cheap:
  * refresh liveness (a long autonomous turn otherwise looks stale on the bus);
  * inject unread URGENT messages that have not been surfaced yet. Each message
    is delivered once (see store.deliveries); non-urgent mail never interrupts
    a turn and waits for the UserPromptSubmit notice.
  * inject machine-wide conditions (RAM pressure, orphaned processes). Same
    once-per-episode discipline, and the same reason for being here: an agent
    about to launch a build on a box that is out of memory has to hear about it
    *before* the next tool call, and this is the only interrupt point there is.
"""

from __future__ import annotations


from cabildo.hooks._common import emit_context, read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return

    from cabildo import notify, store

    conn = store.connect()
    try:
        store.touch(conn, session_id)
        text = notify._join(
            notify.midturn_urgent_text(conn, session_id),
            notify.condition_text(conn, session_id),
        )
    finally:
        conn.close()
    emit_context("PostToolUse", text)

