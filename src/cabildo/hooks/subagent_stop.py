#!/usr/bin/env python3
"""SubagentStop hook: when a spawned subagent (the Task tool) finishes, post a
one-line note to the project channel so its activity is visible in
`cabildo watch`.

Subagents talk to their parent through the harness, never through the bus, so
without this their work never reaches cabildo and the watch stays blank while
they run. The note is addressed to the project channel (like any agent's channel
message), carries the task the subagent was given (its first prompt), and is
sent as the parent session so the roster attribution is right.

Best-effort and silent on any error: a hook must never break the session, and a
failed post is not worth a stack trace in the user's terminal.
"""

from __future__ import annotations

import os
from pathlib import Path

from cabildo.hooks._common import read_input


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return
    cwd = data.get("cwd") or os.getcwd()
    transcript_path = data.get("transcript_path")

    from cabildo import store, transcript
    from cabildo.context import git_project

    task = None
    if transcript_path:
        dig = transcript.session_digest(Path(transcript_path))
        if dig and dig.get("title"):
            task = " ".join(str(dig["title"]).split())[:200]

    root, name = git_project(cwd)
    if not name:
        return  # no project channel to post to
    topic = store.project_topic(name)

    conn = store.connect()
    try:
        row = store.get_session(conn, session_id)
        handle = row["handle"] if row else None
        body = "🤖 subagent finished" + (f": {task}" if task else "")
        store.post_message(
            conn,
            from_session=session_id,
            from_handle=handle,
            body=body,
            topic=topic,
            project=root,
        )
    finally:
        conn.close()
