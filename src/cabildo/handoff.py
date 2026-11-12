"""Handoff notes: when a session that holds a task's lease stops, leave a note in
the task file so the next agent resumes cold instead of re-deriving it.

Two ways in, and files are the store as everywhere:

  * an agent calls `append` deliberately (the reliable path — it states its own
    next step in its own words), exposed as the `agenda_handoff` MCP tool;
  * `on_session_end` auto-appends a lightweight note for any task a session still
    held when it exited, so work abandoned mid-flight is visible, not silent.

A task lease is `issue:<slug>` on the bus but does not encode which register holds
the file, so `locate` looks in the obvious places for the project: its
`docs/issues`, its `docs/agenda`, then the home-wide agenda.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import pathlib

from prodc import issue_store as issues

from . import agenda, store


def _block(text: str, who: str, when: float) -> str:
    stamp = _dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
    return f"\n\n## Handoff — {stamp} ({who})\n{text.strip()}\n"


def append(
    folder: pathlib.Path,
    slug: str,
    text: str,
    *,
    who: str,
    config: issues.Config | None = None,
) -> issues.Issue:
    """Append a handoff block to a task's body, preserving everything already there."""
    cfg = config or issues.load_config(folder)
    issue = issues.load(issues.resolve(folder, slug), cfg)
    updated = dataclasses.replace(issue, body=issue.body.rstrip() + _block(text, who, store._now()))
    issues.save(updated, cfg)
    return updated


def session_task_claims(conn, session_id: str) -> list[tuple[str, str | None]]:
    """(slug, project) for each live task lease a session holds."""
    now = store._now()
    rows = conn.execute(
        "SELECT resource, project FROM claims WHERE holder_session=? AND resource LIKE 'issue:%' "
        "AND expires_at>?",
        (session_id, now),
    )
    return [(r["resource"][len("issue:") :], r["project"]) for r in rows]


def locate(slug: str, project: str | None) -> pathlib.Path | None:
    """The register folder that holds `slug`: the project's docs/issues, then its
    docs/agenda, then the home-wide agenda. None if it is nowhere we look.
    """
    candidates: list[pathlib.Path] = []
    if project:
        found = issues.find_folder(pathlib.Path(project))
        if found:
            candidates.append(found)
        proj_agenda = agenda.project_dir(project)
        if proj_agenda:
            candidates.append(proj_agenda)
    candidates.append(agenda.global_dir())
    for folder in candidates:
        try:
            if folder.is_dir():
                issues.resolve(folder, slug)
                return folder
        except issues.IssueFormatError:
            continue
    return None


def on_session_end(conn, session_id: str, *, last_state: str | None = None) -> list[str]:
    """Auto-note every task a session still held when it ended. Best-effort: a task
    whose file we cannot find or write is skipped, never raised. Call this BEFORE the
    claims are dropped. Returns the slugs noted.
    """
    who = store.display_name(store.get_session(conn, session_id), session_id)
    written: list[str] = []
    for slug, project in session_task_claims(conn, session_id):
        folder = locate(slug, project)
        if folder is None:
            continue
        text = "Session ended while holding this task; no explicit handoff was left."
        if last_state:
            text += f"\nLast: {last_state}"
        try:
            append(folder, slug, text, who=who)
            written.append(slug)
        except Exception:
            continue
    return written
