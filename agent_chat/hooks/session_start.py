#!/usr/bin/env python3
"""SessionStart hook: register this session in the bus and bind session_id to
the owning claude PID (the rendezvous the MCP server later reads), then inject a
status banner naming other active agents and any active locks."""

from __future__ import annotations

import os
import sys

from _common import emit_context, read_input  # noqa: E402  (path injected there)


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
        store.register_session(
            conn, session_id, cwd=cwd, project=root, project_name=name, claude_pid=pid
        )
        banner = notify.session_banner(conn, session_id)
    finally:
        conn.close()
    emit_context("SessionStart", banner)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the session
        print(f"[agent-chat session_start hook error: {e}]", file=sys.stderr)
