"""Codex hook handlers — the Codex counterpart of `cabildo/hooks/`.

Codex invokes `cabildo-codex-hook <Event>` once per lifecycle event with a JSON
payload on **stdin** (snake_case: `session_id`, `cwd`, `tool_name`,
`tool_input`, …) and reads a JSON object on **stdout**. The bus logic —
registration, the claim guard (`cabildo.guard`), unread/urgent delivery
(`cabildo.notify`) — is client-agnostic: it keys on `session_id` + the shared
store, exactly as the Claude hooks do. So these handlers are thin adapters that
translate Codex's I/O contract to and from that shared logic; the enforcement
and delivery decisions themselves are the same code Claude runs.

Identity is bound to the owning `codex` process (`resolve_codex_pid`), the
rendezvous the MCP server reads back via `store.session_for_owner`, so a Codex
session's real id (set here from the hook payload) and its MCP server agree.

Output contract (verified against the Codex hooks docs):

  * inject context   → {"hookSpecificOutput": {"hookEventName": E,
                        "additionalContext": text}}
  * deny a tool call → {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "permissionDecision": "deny", "permissionDecisionReason": r}}
    — identical to Claude Code's PreToolUse shape; Codex does NOT honor
    `continue:false` for PreToolUse.

Enforcement scope on Codex (be honest about it — see README): the Bash-command
guards (push/blanket-stage/branch-switch/destructive-git) fire when Codex's
shell tool presents as `tool_name="Bash"` with a `command` field, and MCP-tool
denials match by name. Codex's native edit tool is `apply_patch`: the guard maps
every path in its envelope (adds, updates, deletes, and both ends of a move) to
`file:` leases, so claim enforcement on Codex *edits* is mutual with Claude Code
— as is claim *visibility* (the guard reads the shared claims table regardless of
who holds the lease).

Never raises: a broken hook must not break the Codex session.
"""

from __future__ import annotations

import json
import os
import sys

CLIENT = "codex"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")


def _read() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def _emit_context(event: str, text: str | None) -> None:
    if not text:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def _register(data: dict) -> str | None:
    """Register/refresh this Codex session, bound to the owning `codex` pid."""
    session_id = data.get("session_id")
    if not session_id:
        return None

    from cabildo import store
    from cabildo.context import git_project, resolve_codex_pid

    cwd = data.get("cwd") or os.getcwd()
    root, name = git_project(cwd)
    pid = resolve_codex_pid()
    conn = store.connect()
    try:
        store.register_session(
            conn,
            session_id,
            cwd=cwd,
            project=root,
            project_name=name,
            client=CLIENT,
            owner_pid=pid,
        )
    finally:
        conn.close()
    return session_id


def session_start(data: dict) -> None:
    sid = _register(data)
    if not sid:
        return
    from cabildo import notify, store

    conn = store.connect()
    try:
        banner = notify.session_banner(conn, sid)
    finally:
        conn.close()
    _emit_context("SessionStart", banner)


def user_prompt_submit(data: dict) -> None:
    sid = _register(data)  # idempotent; also recovers a missed SessionStart
    if not sid:
        return
    from cabildo import notify, store

    conn = store.connect()
    try:
        line = notify._join(notify.unread_line(conn, sid), notify.condition_text(conn, sid))
    finally:
        conn.close()
    _emit_context("UserPromptSubmit", line)


def pre_tool_use(data: dict) -> None:
    session_id = data.get("session_id")
    tool_name = data.get("tool_name")
    if not session_id or not tool_name:
        return

    from cabildo import guard, store

    tool_input = data.get("tool_input") or {}
    conn = store.connect()
    try:
        reason = guard.deny_reason(conn, session_id, tool_name, tool_input)
    finally:
        conn.close()
    if not reason:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def post_tool_use(data: dict) -> None:
    """Mid-turn urgent delivery: inject unread URGENT mail between tool calls,
    the one delivery point Codex documents honoring `additionalContext` on.
    """
    session_id = data.get("session_id")
    if not session_id:
        return
    from cabildo import notify, store

    conn = store.connect()
    try:
        text = notify.midturn_urgent_text(conn, session_id)
    finally:
        conn.close()
    _emit_context("PostToolUse", text)


_HANDLERS = {
    "SessionStart": session_start,
    "UserPromptSubmit": user_prompt_submit,
    "PreToolUse": pre_tool_use,
    "PostToolUse": post_tool_use,
}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = _HANDLERS.get(name)
    if handler is None:
        print(
            f"cabildo-codex-hook: unknown event {name!r} (choose from: {', '.join(EVENTS)})",
            file=sys.stderr,
        )
        return
    data = _read()
    try:
        handler(data)
    except Exception as e:  # a broken hook must never break the Codex session
        print(f"[cabildo codex {name} hook error: {e}]", file=sys.stderr)


if __name__ == "__main__":
    main()
