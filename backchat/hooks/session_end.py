#!/usr/bin/env python3
"""SessionEnd hook: mark this session ended and drop any advisory locks it held
so a finished agent never blocks the others. Injects nothing."""

from __future__ import annotations


from backchat.hooks._common import read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return

    from backchat import store

    conn = store.connect()
    try:
        store.end_session(conn, session_id)
    finally:
        conn.close()

