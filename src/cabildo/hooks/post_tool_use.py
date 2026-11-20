#!/usr/bin/env python3
"""PostToolUse hook: mid-turn mail delivery. Fires after every tool call, which
is the only interrupt point Claude Code gives us inside a turn — an agent that
is thinking or writing text cannot be reached until its next tool call.

Four jobs, all cheap:
  * refresh liveness (a long autonomous turn otherwise looks stale on the bus);
  * inject unread URGENT messages that have not been surfaced yet. Each message
    is delivered once (see store.deliveries); non-urgent mail never interrupts
    a turn and waits for the UserPromptSubmit notice.
  * inject machine-wide conditions (RAM pressure, orphaned processes). Same
    once-per-episode discipline, and the same reason for being here: an agent
    about to launch a build on a box that is out of memory has to hear about it
    *before* the next tool call, and this is the only interrupt point there is.
  * when a `git push` was just rejected non-fast-forward, inject the fetch+rebase
    recovery recipe — so the agent does not reach for `reset --hard`/`--force`.
"""

from __future__ import annotations

from typing import Any

from cabildo.hooks._common import emit_context, read_input


def _response_text(resp: Any) -> str:
    """Flatten a PostToolUse ``tool_response`` to text. Claude Code hands a dict
    (``stdout``/``stderr``/…); some clients hand a bare string. Only the string
    fields matter for a text match, and a shape we don't recognise yields "".
    """
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return " ".join(v for v in resp.values() if isinstance(v, str))
    return ""


def main() -> None:
    data = read_input()
    session_id = data.get("session_id")
    if not session_id:
        return

    cwd = data.get("cwd")

    from cabildo import config, guard, notify, store

    recipe = None
    if data.get("tool_name") == "Bash":
        command = (data.get("tool_input") or {}).get("command") or ""
        recipe = guard.push_failure_recipe(command, _response_text(data.get("tool_response")))

    conn = store.connect()
    try:
        store.touch(conn, session_id, cwd=cwd)
        if guard.writes_files(str(data.get("tool_name") or ""), data.get("tool_input") or {}):
            store.mark_wrote(conn, session_id)
        text = notify._join(
            notify.midturn_urgent_text(conn, session_id),
            notify.condition_text(conn, session_id),
            notify.winddown_text(conn, session_id),
            notify.timeline_text(conn, session_id, config.timeline_interval(cwd)),
            recipe,
        )
    finally:
        conn.close()
    emit_context("PostToolUse", text)
