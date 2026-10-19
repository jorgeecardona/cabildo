"""Minutas: a daily conversation record per project — each session's title, its
turns, and where it left off — written to `<cabildo home>/minutes/<project>/<date>.md`
so a day's conversations across every session push in one file.

Two sources, DB first. When recording is on (`[record] messages` + the daemon's
--save-messages), a day is rendered from the turns cabildo ingested into its own
DB, which is client-agnostic (whatever was recorded, not only Claude). Otherwise
it falls back to reading Claude Code's transcripts on disk via `transcript.py`.
The transcript fallback is Claude-only and best-effort: a session with no
transcript (Codex, opencode, a pure sub-agent) is skipped, never an error.

The SessionEnd hook writes a session's block as it ends; `cabildo minutes`
regenerates a project's day on demand.
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


def _day_bounds(day: str) -> tuple[float, float]:
    """[start, end) epoch seconds for a local calendar day."""
    d = _dt.date.fromisoformat(day)
    start = _dt.datetime.combine(d, _dt.time.min).timestamp()
    end = _dt.datetime.combine(d + _dt.timedelta(days=1), _dt.time.min).timestamp()
    return start, end


def render_session_db(session_id: str, rows: list[Any]) -> str:
    """One session's block from recorded DB turns. Same shape as render_session,
    plus the model in the header, which the DB knows and a raw transcript title
    does not.
    """
    first_user = next((r for r in rows if r["role"] == "user"), None)
    last_user = next((r for r in reversed(rows) if r["role"] == "user"), None)
    title = (first_user["body"] if first_user else rows[0]["body"]).strip()[:120]
    model = next((r["model"] for r in rows if r["model"]), None)
    last_ts = rows[-1]["ts"]
    when = _dt.datetime.fromtimestamp(last_ts).strftime("%H:%M") if last_ts else "?"
    tag = f" · {model}" if model else ""
    lines = [
        f"## {title}",
        f"*session {session_id[:8]} · {len(rows)} turns{tag} · last active {when}*",
        "",
    ]
    for r in rows:
        who = "🧑" if r["role"] == "user" else "🤖"
        text = " ".join(r["body"].split())[:CAP_CHARS]
        lines.append(f"- {who} {text}")
    if last_user:
        lines += ["", f"**Left off:** {' '.join(last_user['body'].split())[:CAP_CHARS]}"]
    return "\n".join(lines) + "\n"


def render_project_day_from_db(conn, project_name: str | None, day: str) -> str | None:
    """A project's day rendered from the recorded turns in the bus DB, or None
    when nothing was recorded for that project on that day (so the caller can
    fall back to the transcripts). Sessions are newest-active first.
    """
    start, end = _day_bounds(day)
    rows = store.transcript_messages_between(conn, project_name, start, end)
    if not rows:
        return None
    groups: dict[str, list[Any]] = {}
    for r in rows:
        groups.setdefault(r["session_id"], []).append(r)
    ordered = sorted(groups.items(), key=lambda kv: kv[1][-1]["ts"] or 0, reverse=True)
    header = f"# Minutas — {project_name or 'unknown'} — {day}\n\n"
    return header + "\n".join(render_session_db(sid, rws) for sid, rws in ordered)


def render_project_day(
    project_name: str | None,
    project_root: str | None,
    day: str | None = None,
    conn: Any = None,
) -> str:
    """A project's minutes for a day. Prefers cabildo's own recorded turns when
    the day was recorded (client-agnostic: whatever was ingested, not only
    Claude), and falls back to the transcripts on disk otherwise.
    """
    day = day or _dt.date.today().isoformat()
    close = conn is None
    c = conn or store.connect()
    try:
        from_db = render_project_day_from_db(c, project_name, day)
    finally:
        if close:
            c.close()
    if from_db is not None:
        return from_db
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
