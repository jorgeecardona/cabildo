"""Minutas: a daily conversation record per project, distilled from the transcripts
Claude Code already writes. cabildo does not log conversations itself; it reads those
transcripts (via `transcript.py`) and writes a readable digest — each session's title,
its turns, and where it left off — to `<cabildo home>/minutes/<project>/<date>.md`, so
a day's conversations across every session land in one file.

Claude-only and best-effort: a session with no transcript (Codex, opencode, a pure
sub-agent) is simply skipped, never an error. The SessionEnd hook writes a session's
block as it ends; `cabildo minutes` regenerates a project's day on demand.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from typing import Any

from . import store, transcript

MAX_TURNS = 60
CAP_CHARS = 600


def minutes_dir() -> pathlib.Path:
    return store.home() / "minutes"


def _transcript_path(session_id: str, project_root: str | None) -> pathlib.Path | None:
    if not project_root:
        return None
    path = (
        transcript.transcript_root() / transcript.project_slug(project_root) / f"{session_id}.jsonl"
    )
    return path if path.exists() else None


def session_minutes(path: pathlib.Path) -> dict[str, Any] | None:
    """Title, turns and where-left-off for one transcript, or None if it has no real
    conversation.
    """
    digest = transcript.session_digest(path)
    if digest is None:
        return None
    turns, _ = transcript.session_turns(path, cap_chars=CAP_CHARS, max_turns=MAX_TURNS)
    return {
        "session_id": digest["session_id"],
        "title": digest["title"],
        "turns": turns,
        "last": digest["last"],
        "mtime": digest["mtime"],
    }


def render_session(m: dict[str, Any]) -> str:
    """One session's block: a heading, the turns, and where it left off."""
    when = _dt.datetime.fromtimestamp(m["mtime"]).strftime("%H:%M")
    lines = [
        f"## {m['title'].strip()[:120]}",
        f"*session {m['session_id'][:8]} · {len(m['turns'])} turns · last active {when}*",
        "",
    ]
    for t in m["turns"]:
        who = "🧑" if t["role"] == "user" else "🤖"
        text = " ".join(t["text"].split())[:CAP_CHARS]
        lines.append(f"- {who} {text}")
    if m["last"]:
        lines += ["", f"**Left off:** {' '.join(m['last'].split())[:CAP_CHARS]}"]
    return "\n".join(lines) + "\n"


def _day_file(project_name: str | None, day: str) -> pathlib.Path:
    return minutes_dir() / (project_name or "unknown") / f"{day}.md"


def write_session(
    session_id: str, project_root: str | None, project_name: str | None, *, day: str | None = None
) -> pathlib.Path | None:
    """Append one session's block to its project's day file. Idempotent per session:
    a block already recorded for this session is not duplicated. Returns the file, or
    None when there is no transcript to record.
    """
    path = _transcript_path(session_id, project_root)
    if path is None:
        return None
    m = session_minutes(path)
    if m is None:
        return None
    out = _day_file(project_name, day or _dt.date.today().isoformat())
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = out.read_text() if out.exists() else ""
    marker = f"<!-- session {session_id} -->"
    if marker in existing:
        return out  # already recorded
    header = f"# Minutas — {project_name or 'unknown'} — {day or _dt.date.today().isoformat()}\n\n"
    out.write_text((existing or header) + marker + "\n" + render_session(m) + "\n")
    return out


def render_project_day(
    project_name: str | None, project_root: str | None, day: str | None = None
) -> str:
    """Regenerate a project's minutes for a day from the transcripts on disk, newest
    session first. Independent of what the hook has written.
    """
    day = day or _dt.date.today().isoformat()
    slug = transcript.project_slug(project_root) if project_root else None
    sessions = transcript.list_sessions(slug=slug)
    blocks: list[str] = []
    for dig in sessions:
        when = _dt.datetime.fromtimestamp(dig["mtime"]).strftime("%Y-%m-%d")
        if when != day:
            continue
        path = transcript.transcript_root() / dig["project_dir"] / f"{dig['session_id']}.jsonl"
        m = session_minutes(path)
        if m:
            blocks.append(render_session(m))
    header = f"# Minutas — {project_name or 'unknown'} — {day}\n\n"
    return header + "\n".join(blocks) if blocks else header + "_no recorded sessions_\n"
