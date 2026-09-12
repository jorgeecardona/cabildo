#!/usr/bin/env python3
"""UserPromptSubmit hook: refresh liveness + re-assert the PID rendezvous, then
inject a compact unread notice (only when there is something unread). This is
the passive "you have mail" delivery that runs on every user turn.
"""

from __future__ import annotations

import os

from cabildo.hooks._common import emit_context, read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return
    cwd = data.get("cwd") or os.getcwd()

    from cabildo import notify, store
    from cabildo.context import git_project, resolve_claude_pid

    root, name = git_project(cwd)
    pid = resolve_claude_pid()
    conn = store.connect()
    try:
        # Idempotent: keeps last_seen fresh and re-binds pid->session_id in case
        # SessionStart was missed (e.g. hook added mid-session).
        store.register_session(
            conn,
            session_id,
            cwd=cwd,
            project=root,
            project_name=name,
            claude_pid=pid,
            tag=os.environ.get("CABILDO_TAG"),
        )
        # Conditions too: a turn that never makes a tool call would otherwise
        # never reach PostToolUse, and "the machine is out of RAM" is exactly
        # the kind of thing the user's next instruction depends on.
        line = notify._join(
            notify.unread_line(conn, session_id),
            notify.condition_text(conn, session_id),
        )
    finally:
        conn.close()
    emit_context("UserPromptSubmit", line)
