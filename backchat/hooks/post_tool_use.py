#!/usr/bin/env python3
"""PostToolUse hook: mid-turn mail delivery. Fires after every tool call, which
is the only interrupt point Claude Code gives us inside a turn — an agent that
is thinking or writing text cannot be reached until its next tool call.

Two jobs, both cheap:
  * refresh liveness (a long autonomous turn otherwise looks stale on the bus);
  * inject unread URGENT messages that have not been surfaced yet. Each message
    is delivered once (see store.deliveries); non-urgent mail never interrupts
    a turn and waits for the UserPromptSubmit notice.
"""

from __future__ import annotations


from backchat.hooks._common import emit_context, read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return

    from backchat import notify, store

    conn = store.connect()
    try:
        store.touch(conn, session_id)
        text = notify.midturn_urgent_text(conn, session_id)
    finally:
        conn.close()
    emit_context("PostToolUse", text)

