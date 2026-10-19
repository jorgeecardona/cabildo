#!/usr/bin/env python3
"""SessionEnd hook: leave a handoff note on any task this session still held, then
mark it ended and drop its advisory locks so a finished agent never blocks the
others. The handoff runs BEFORE the locks are dropped, since it reads them. Injects
nothing.
"""

from __future__ import annotations

from cabildo.hooks._common import read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return

    from cabildo import store

    conn = store.connect()
    try:
        try:
            from cabildo import handoff

            handoff.on_session_end(conn, session_id)  # note tasks left mid-flight
        except Exception:
            pass  # a handoff must never keep a session from ending cleanly
        try:
            from cabildo import minutes

            me = store.get_session(conn, session_id)
            if me:
                minutes.write_session(session_id, me["project"], me["project_name"])
        except Exception:
            pass  # minutas are best-effort; never block a session ending
        store.end_session(conn, session_id)
    finally:
        conn.close()
