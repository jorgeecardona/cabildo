#!/usr/bin/env python3
"""SessionStart hook: register this session in the bus and bind session_id to
the owning claude PID (the rendezvous the MCP server later reads), then inject a
status banner naming other active agents and any active locks."""

from __future__ import annotations

import os

from backchat.hooks._common import emit_context, read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return
    cwd = data.get("cwd") or os.getcwd()

    from backchat import store
    from backchat.context import git_project, resolve_claude_pid
    from backchat import notify

    root, name = git_project(cwd)
    pid = resolve_claude_pid()
    conn = store.connect()
    try:
        store.register_session(
            conn, session_id, cwd=cwd, project=root, project_name=name, claude_pid=pid,
            tag=os.environ.get("BACKCHAT_TAG"),
        )
        banner = notify.session_banner(conn, session_id)
    finally:
        conn.close()
    emit_context("SessionStart", banner)

