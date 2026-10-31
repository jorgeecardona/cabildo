#!/usr/bin/env python3
"""StopFailure hook: when a turn ends on a rate limit, mark the session OUT of tokens.

Claude Code runs this instead of Stop when the turn ends on an API error. The hook
input has no reset time, so it is read from the transcript's last rate-limit record.
Only ``error == "rate_limit"`` is handled; other errors are not a token limit. The hook
output is ignored by Claude Code, so this only writes to the bus.
"""

from __future__ import annotations

from cabildo.hooks._common import read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id or data.get("error") != "rate_limit":
        return

    from cabildo import store, transcript

    found = (
        transcript.last_quota_rejection(data["transcript_path"])
        if data.get("transcript_path")
        else None
    )
    conn = store.connect()
    try:
        store.mark_out_of_tokens(
            conn,
            session_id,
            kind=found["kind"] if found else "unknown",
            resets_at=found["resets_at"] if found else 0.0,
        )
    finally:
        conn.close()
