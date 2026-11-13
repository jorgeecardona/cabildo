#!/usr/bin/env python3
"""SessionEnd hook: mark the session ended and drop its advisory locks so a
finished agent never blocks the others. Injects nothing.
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
            from cabildo import minutes

            me = store.get_session(conn, session_id)
            if me:
                minutes.write_session(session_id, me["project"], me["project_name"])
        except Exception:
            pass  # minutas are best-effort; never block a session ending
        store.end_session(conn, session_id)
    finally:
        conn.close()
