"""Builders for transcript lines (the JSONL shapes Claude writes)."""

from __future__ import annotations

PROJECT = "/work/proj"  # the working dir the list is opened for


def user(text: str, cwd: str = PROJECT, branch: str = "main") -> dict:
    return {
        "type": "user",
        "cwd": cwd,
        "gitBranch": branch,
        "message": {"role": "user", "content": text},
    }


def assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def ai_title(title: str) -> dict:
    return {"type": "ai-title", "aiTitle": title}


def custom_title(title: str) -> dict:
    return {"type": "custom-title", "customTitle": title}


def continued_in(session_id: str) -> dict:
    return {"type": "continued-in", "continuedInSessionId": session_id}


# --- lines in Claude's own key order (2.1.302), for the raw-line parser ---------------


def claude_user(
    sid: str,
    text: str,
    cwd: str,
    branch: str,
    parent: str | None = "5d1c2a4e-0b7f-4c1e-9d0a-3f6b8e2c7a11",
) -> dict:
    return {
        "parentUuid": parent,
        "isSidechain": False,
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": "8e0f6c2d-1a3b-4c5d-9e7f-0a1b2c3d4e5f",
        "timestamp": "2026-10-18T10:00:00Z",
        "permissionMode": "auto",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": cwd,
        "sessionId": sid,
        "version": "2.1.302",
        "gitBranch": branch,
    }


def claude_assistant(sid: str, text: str, cwd: str, branch: str) -> dict:
    return {
        "parentUuid": "8e0f6c2d-1a3b-4c5d-9e7f-0a1b2c3d4e5f",
        "isSidechain": False,
        "message": {
            "model": "claude",
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "requestId": "req_1",
        "type": "assistant",
        "uuid": "9f1a7d3e-2b4c-5d6e-0f8a-1b2c3d4e5f60",
        "timestamp": "2026-10-18T10:00:01Z",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": cwd,
        "sessionId": sid,
        "version": "2.1.302",
        "gitBranch": branch,
    }


def reply(
    msg_id: str,
    prompt: tuple[int, int, int],
    output: int,
    sidechain: bool = False,
    text: str = "ok",
    model: str = "claude-opus-5-5",
    effort: str = "",
) -> dict:
    """An assistant line with ``usage``, in Claude's key order (2.1.302). ``prompt`` is
    (input, cache writes, cache reads). ``effort`` goes after the message, as in 2.1.310.
    """
    fresh, write, read = prompt
    counts = {
        "input_tokens": fresh,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": output,
    }
    return {
        "parentUuid": "8e0f6c2d-1a3b-4c5d-9e7f-0a1b2c3d4e5f",
        "isSidechain": sidechain,
        "message": {
            "model": model,
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {
                **counts,
                "cache_creation": {"ephemeral_1h_input_tokens": write},
                "iterations": [{**counts, "type": "message"}],
            },
        },
        "type": "assistant",
        **({"effort": effort, "perTurnEffort": effort} if effort else {}),
    }
