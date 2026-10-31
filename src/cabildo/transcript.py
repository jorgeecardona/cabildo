"""Scan Claude Code transcripts for cabildo tool traffic.

The hook_metrics table measures what cabildo *pushes* into a session (banners,
unread lines). It cannot see the other half of the cost: the tokens an agent
spends *calling* cabildo tools and reading their results — that lives only in
Claude Code's own transcripts (~/.claude/projects/<slug>/<session>.jsonl, one
JSON event per line).

This module reads those transcripts and sums the character footprint of every
`mcp__cabildo__*` tool call plus its result. It is an approximation (billed
tokens are per-message, not per-tool-call), but the input+output chars of
cabildo tool traffic is an honest measure of the context those tools consumed.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CHARS_PER_TOKEN = 4  # rough industry rule of thumb; good enough for a cost gauge
_CABILDO = "cabildo"


def transcript_root() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


def _content_len(content: Any) -> int:
    """Length of a message/result content, whether it's a plain string or the
    structured list-of-blocks form Claude Code also uses.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, str):
                total += len(b)
            elif isinstance(b, dict):
                t = b.get("text") or b.get("content")
                total += len(t) if isinstance(t, str) else len(json.dumps(b))
        return total
    if content is None:
        return 0
    return len(json.dumps(content))


def scan_file(path: Path) -> dict[str, dict[str, int]]:
    """Per-tool cabildo traffic in one transcript. Returns
    {tool_name: {"calls", "input_chars", "output_chars"}}.

    tool_use and its tool_result are on separate lines, so we remember each
    cabildo tool_use id and attribute the later result back to it.
    """
    by_tool: dict[str, dict[str, int]] = {}
    id2name: dict[str, str] = {}

    def bucket(name: str) -> dict[str, int]:
        return by_tool.setdefault(name, {"calls": 0, "input_chars": 0, "output_chars": 0})

    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return by_tool
    with fh:
        for line in fh:
            line = line.strip()
            # Cheap prefilter: only tool_use / tool_result lines can matter, and
            # both contain the substring "tool_use" (tool_result blocks carry a
            # tool_use_id). Filtering on "cabildo" would wrongly drop result
            # lines whose payload doesn't echo the tool name.
            if not line or "tool_use" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "tool_use":
                    name = b.get("name", "")
                    if _CABILDO in name:
                        id2name[b.get("id", "")] = name
                        e = bucket(name)
                        e["calls"] += 1
                        e["input_chars"] += _content_len(b.get("input"))
                elif t == "tool_result":
                    name = id2name.get(b.get("tool_use_id", ""))
                    if name:
                        bucket(name)["output_chars"] += _content_len(b.get("content"))
    return by_tool


def scan(root: Path | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Aggregate cabildo tool traffic across all transcripts (or one session).

    Returns totals, a per-tool breakdown, and the file count scanned.
    """
    root = root or transcript_root()
    by_tool: dict[str, dict[str, int]] = {}
    files = 0
    if root.is_dir():
        if session_id:
            paths = list(root.glob(f"**/{session_id}.jsonl"))
        else:
            paths = list(root.glob("**/*.jsonl"))
        for path in paths:
            per = scan_file(path)
            if per:
                files += 1
            for name, e in per.items():
                agg = by_tool.setdefault(name, {"calls": 0, "input_chars": 0, "output_chars": 0})
                for k, v in e.items():
                    agg[k] += v

    calls = sum(e["calls"] for e in by_tool.values())
    input_chars = sum(e["input_chars"] for e in by_tool.values())
    output_chars = sum(e["output_chars"] for e in by_tool.values())
    return {
        "files": files,
        "calls": calls,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "tokens_est": (input_chars + output_chars) // CHARS_PER_TOKEN,
        "by_tool": by_tool,
    }


# --------------------------------------------------------------------------- #
# Session digests: title + "where I left off" from a transcript
#
# The transcript IS the durable record of a conversation — Claude Code writes it
# on every turn with no help from us. To recover a session you lost among ten
# open tabs you need two things it already contains: a human-readable label (the
# first thing you typed) and where you stopped (the last thing you typed). Both
# fall straight out of the user turns; no model call, no capture discipline, and
# it works retroactively on every transcript already on disk.
# --------------------------------------------------------------------------- #
def project_slug(path: str) -> str:
    """Claude Code's transcript-directory name for a project root.

    Claude stores each project's transcripts under
    ~/.claude/projects/<slug>/, where <slug> is the absolute cwd with every
    non-alphanumeric character turned into a dash
    (/home/jo/my.app -> -home-jo-my-app). Recomputing it is how we find *this*
    project's sessions without a lookup table.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def _user_text(obj: Any) -> str | None:
    """The human-typed text of one transcript line, or None if the line is not a
    genuine user turn.

    A transcript's `type=="user"` lines are a mixed bag: real prompts, but also
    tool_result blocks (they ride on user-type lines), hook/meta injections, and
    slash-command echoes. We want only what a person actually typed, so
    tool_result lines, meta lines, and the wrapper forms (`<...>` tags,
    hook output, the caveat banner) are all rejected.
    """
    if not isinstance(obj, dict) or obj.get("type") != "user" or obj.get("isMeta"):
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "tool_result":
                    return None  # a tool result, not something the human typed
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        text = "".join(parts)
    else:
        return None
    text = text.strip()
    if not text:
        return None
    low = text.lower()
    if (
        text.startswith("<")
        or text.startswith("[Request interrupted")  # Claude Code interrupt marker
        or "hookspecificoutput" in low
        or low.startswith("caveat:")
    ):
        return None
    return text


def _assistant_text(obj: Any) -> str | None:
    """What Claude actually *said* on one transcript line, or None.

    An `type=="assistant"` line's content is a list of blocks; we keep only the
    `text` blocks (the spoken reply) and drop `tool_use` blocks — those are the
    cost side of the turn, not part of the conversation a human would read.
    """
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
    text = "".join(parts).strip()
    return text or None


def _turn(obj: Any) -> tuple[str, str] | None:
    """(role, text) for a genuine conversation turn — a human prompt ('user') or
    Claude's spoken reply ('assistant') — else None. Tool calls, tool results,
    and meta/hook lines are not turns and return None.
    """
    t = obj.get("type") if isinstance(obj, dict) else None
    if t == "user":
        text = _user_text(obj)
        return ("user", text) if text else None
    if t == "assistant":
        text = _assistant_text(obj)
        return ("assistant", text) if text else None
    return None


def session_turns(
    path: Path,
    after_bytes: int = 0,
    cap_chars: int = 4000,
    max_turns: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """New conversation turns in a transcript since byte offset `after_bytes`,
    and the offset to resume from next time.

    Transcripts are append-only, so a byte offset is a stable, restart-safe
    cursor: seek to it and read forward. Two edges are handled so we never lose
    or double-count a turn:
      * a trailing line with no newline is a turn still being written — we stop
        before it and report the offset at its start, to re-read it once whole;
      * if the file is smaller than the saved offset (rotated/truncated) we
        restart from the top.
    Each turn is {"role", "text", "ts"}; text is capped to `cap_chars`. At most
    `max_turns` per call — the offset stops at the last one taken, so the tail
    ships on the next pass.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], after_bytes
    if after_bytes > size:
        after_bytes = 0
    turns: list[dict[str, Any]] = []
    offset = after_bytes
    try:
        fh = path.open("rb")
    except OSError:
        return [], after_bytes
    with fh:
        fh.seek(after_bytes)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # partial final line — leave cursor before it
            if len(turns) >= max_turns:
                break  # tail resumes here next pass
            offset += len(raw)
            line = raw.decode("utf-8", "replace")
            # Cheap gate: only user/assistant lines can be turns.
            if '"type":"user"' not in line and '"type":"assistant"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            got = _turn(obj)
            if got is None:
                continue
            role, text = got
            if len(text) > cap_chars:
                text = text[:cap_chars] + "…"
            ts = obj.get("timestamp") if isinstance(obj, dict) else None
            turns.append({"role": role, "text": text, "ts": ts})
    return turns, offset


def _epoch(ts: Any) -> float | None:
    """An ISO-8601 transcript timestamp as epoch seconds, or None."""
    if not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def session_messages(
    path: Path,
    after_bytes: int = 0,
    cap_chars: int = 20000,
    max_turns: int = 500,
) -> tuple[list[dict[str, Any]], int]:
    """Like `session_turns`, but for persisting into the DB: each turn also
    carries the transcript entry's `uuid` (dedup key) and the assistant `model`,
    and `ts` is epoch seconds. Same append-only byte-offset cursor and same
    partial-final-line / truncation handling, so it never loses or double-counts.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], after_bytes
    if after_bytes > size:
        after_bytes = 0
    out: list[dict[str, Any]] = []
    offset = after_bytes
    try:
        fh = path.open("rb")
    except OSError:
        return [], after_bytes
    with fh:
        fh.seek(after_bytes)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # partial final line — leave the cursor before it
            if len(out) >= max_turns:
                break
            offset += len(raw)
            line = raw.decode("utf-8", "replace")
            if '"type":"user"' not in line and '"type":"assistant"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            got = _turn(obj)
            if got is None:
                continue
            uid = obj.get("uuid") if isinstance(obj, dict) else None
            if not uid:
                continue  # no stable id to dedup on — skip rather than duplicate
            role, text = got
            if len(text) > cap_chars:
                text = text[:cap_chars] + "…"
            model = None
            msg = obj.get("message") if isinstance(obj, dict) else None
            if role == "assistant" and isinstance(msg, dict):
                model = msg.get("model")
            out.append(
                {
                    "uuid": uid,
                    "role": role,
                    "text": text,
                    "ts": _epoch(obj.get("timestamp")),
                    "model": model,
                }
            )
    return out, offset


def session_digest(path: Path) -> dict[str, Any] | None:
    """First and last real user turns of a transcript, its turn count, and the
    file's mtime. The first turn is a natural title; the last is where you left
    off. Returns None for a transcript with no genuine user turn (e.g. a pure
    sub-agent log).
    """
    first: str | None = None
    last: str | None = None
    turns = 0
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            # Compact machine-written JSON: a genuine user line carries the exact
            # substring `"type":"user"`. Cheap gate before the json parse.
            if '"type":"user"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            text = _user_text(obj)
            if text is None:
                continue
            turns += 1
            if first is None:
                first = text
            last = text
    if first is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "session_id": path.stem,
        "title": first,
        "last": last,
        "turns": turns,
        "mtime": mtime,
    }


def list_sessions(root: Path | None = None, slug: str | None = None) -> list[dict[str, Any]]:
    """Session digests for one project (by `slug`) or across all projects,
    newest first.

    Only top-level `*.jsonl` files in each project directory are scanned:
    sub-agent sidechain transcripts live in subdirectories, and those are not
    sessions you resume, so the non-recursive glob leaves them out.
    """
    root = root or transcript_root()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    dirs = [root / slug] if slug else [d for d in root.iterdir() if d.is_dir()]
    for d in dirs:
        if not d.is_dir():
            continue
        for path in d.glob("*.jsonl"):
            dig = session_digest(path)
            if dig:
                dig["project_dir"] = d.name
                out.append(dig)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# How much of the transcript's end is read to find the rejection: the synthetic
# rate-limit record is the last thing Claude Code writes when a turn is refused.
_TAIL_BYTES = 256 * 1024


def last_quota_rejection(path: str | Path) -> dict[str, Any] | None:
    """The newest rate-limit rejection in a transcript, or None.

    Claude Code writes a synthetic assistant record with ``quotaLimits.status ==
    "rejected"`` when a turn is refused. Returns ``{"kind": <rateLimitType>, "resets_at":
    <epoch seconds, 0 if absent>}``. Reads only the tail of the file.
    """
    try:
        with Path(path).open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if '"quotaLimits"' not in line:
            continue
        try:
            quota = json.loads(line).get("quotaLimits")
        except ValueError:
            continue
        if isinstance(quota, dict) and quota.get("status") == "rejected":
            return {
                "kind": str(quota.get("rateLimitType") or "unknown"),
                "resets_at": float(quota.get("resetsAt") or 0),
            }
    return None
