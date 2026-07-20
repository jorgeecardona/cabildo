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
from pathlib import Path
from typing import Any

CHARS_PER_TOKEN = 4  # rough industry rule of thumb; good enough for a cost gauge
_CABILDO = "cabildo"


def transcript_root() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


def _content_len(content: Any) -> int:
    """Length of a message/result content, whether it's a plain string or the
    structured list-of-blocks form Claude Code also uses."""
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
    cabildo tool_use id and attribute the later result back to it."""
    by_tool: dict[str, dict[str, int]] = {}
    id2name: dict[str, str] = {}

    def bucket(name: str) -> dict[str, int]:
        return by_tool.setdefault(
            name, {"calls": 0, "input_chars": 0, "output_chars": 0}
        )

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

    Returns totals, a per-tool breakdown, and the file count scanned."""
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
                agg = by_tool.setdefault(
                    name, {"calls": 0, "input_chars": 0, "output_chars": 0}
                )
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
