#!/usr/bin/env python3
"""SessionEnd hook: mark this session ended and drop any advisory locks it held
so a finished agent never blocks the others. Injects nothing."""

from __future__ import annotations

import sys

from _common import read_input  # noqa: E402


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return

    from agent_chat import store

    conn = store.connect()
    try:
        store.end_session(conn, session_id)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[agent-chat session_end hook error: {e}]", file=sys.stderr)
