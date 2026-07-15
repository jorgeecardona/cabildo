#!/usr/bin/env python3
"""UserPromptSubmit hook: refresh liveness + re-assert the PID rendezvous, then
inject a compact unread notice (only when there is something unread). This is
the passive "you have mail" delivery that runs on every user turn."""

from __future__ import annotations

import os
import sys

from _common import emit_context, read_input  # noqa: E402


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return
    cwd = data.get("cwd") or os.getcwd()

    from agent_chat import store
    from agent_chat.context import git_project, resolve_claude_pid
    from agent_chat import notify

    root, name = git_project(cwd)
    pid = resolve_claude_pid()
    conn = store.connect()
    try:
        # Idempotent: keeps last_seen fresh and re-binds pid->session_id in case
        # SessionStart was missed (e.g. hook added mid-session).
        store.register_session(
            conn, session_id, cwd=cwd, project=root, project_name=name, claude_pid=pid
        )
        line = notify.unread_line(conn, session_id)
    finally:
        conn.close()
    emit_context("UserPromptSubmit", line)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[agent-chat prompt_submit hook error: {e}]", file=sys.stderr)
