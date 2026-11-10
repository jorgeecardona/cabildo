"""record.py: persist conversation turns from transcripts into cabildo's DB.

The daemon calls `ingest` on each pass when started with --save-messages. It
walks recently-seen sessions, and for each one whose project has opted in
(`[record] messages = true`), reads the new tail of its transcript and writes the
turns to `transcript_messages`. A per-session byte cursor means every pass reads
only what is new; `uuid` dedup makes a re-read harmless.

Claude-only today: it reads Claude Code's transcript JSONL. Other clients (codex,
astra, ...) keep their transcripts in their own formats and would each need their
own reader; the DB schema is client-agnostic and ready for them.
"""

from __future__ import annotations

from . import config, store, transcript

# How far back to look for sessions worth ingesting. A session ended within this
# window still has a tail (its final turns push after the last hook), so the
# ingest is not limited to active sessions. Older sessions are done and their
# cursor already sits at end-of-file, so revisiting them buys nothing.
INGEST_WINDOW = 6 * 3600.0


def _transcript_path(session_id: str, project_root: str | None):
    if not project_root:
        return None
    return (
        transcript.transcript_root() / transcript.project_slug(project_root) / f"{session_id}.jsonl"
    )


def ingest(conn, *, window: float = INGEST_WINDOW, log=lambda _m: None) -> int:
    """One ingest pass. Returns the number of new turns written across all
    opted-in sessions. Never raises: a bad transcript for one session must not
    stop the daemon or the other sessions.
    """
    total = 0
    since = store._now() - window
    for sess in store.sessions_since(conn, since):
        client = sess["client"] or "claude"
        if client != "claude":
            continue  # only Claude transcripts are readable here (see module docstring)
        cwd = sess["cwd"]
        project = sess["project"]
        project_name = sess["project_name"]
        if not config.record_messages(cwd, project_name):
            continue
        path = _transcript_path(sess["session_id"], project)
        if path is None or not path.exists():
            continue
        try:
            offset = store.get_ingest_cursor(conn, sess["session_id"])
            rows, new_offset = transcript.session_messages(path, offset)
            if rows:
                n = store.record_transcript_messages(
                    conn,
                    sess["session_id"],
                    rows,
                    client=client,
                    project=project,
                    project_name=project_name,
                )
                total += n
                if n:
                    log(f"  recorded {n} turn(s) from {store.display_name(sess)}")
                # The newest assistant model in this batch is this session's
                # current model, an agent-card field peers read via list_agents.
                model = next((r["model"] for r in reversed(rows) if r.get("model")), None)
                if model:
                    store.set_session_model(conn, sess["session_id"], model)
            if new_offset != offset:
                store.set_ingest_cursor(conn, sess["session_id"], new_offset)
        except Exception as e:  # one broken transcript must not poison the pass
            log(f"  (ingest skipped {sess['session_id'][:8]}: {type(e).__name__}: {e})")
    return total


def _transcript_cwd(path) -> str | None:
    """The ``cwd`` the transcript recorded, from its first lines that carry one."""
    import json

    with path.open(encoding="utf-8", errors="replace") as handle:
        for _, line in zip(range(50), handle, strict=False):
            try:
                cwd = json.loads(line).get("cwd")
            except (ValueError, AttributeError):
                continue
            if isinstance(cwd, str) and cwd:
                return cwd
    return None


def import_file(conn, path) -> int:
    """Record every turn of one transcript file. Returns how many were new.

    Reads the whole file, ``max_turns`` at a time. The session id is the file name. Safe
    to repeat: ``uuid`` dedup skips turns already recorded. Ignores ``[record]``: the
    person running the import has asked for it.
    """
    from .context import git_project

    cwd = _transcript_cwd(path)
    project, project_name = git_project(cwd) if cwd else (None, None)
    total, offset = 0, 0
    while True:
        rows, new_offset = transcript.session_messages(path, offset)
        if rows:
            total += store.record_transcript_messages(
                conn, path.stem, rows, project=project, project_name=project_name
            )
        if new_offset == offset:
            return total
        offset = new_offset


def import_all(conn, roots, *, log=lambda _m: None) -> int:
    """Record every ``*.jsonl`` transcript under ``roots``. Never raises for one bad file."""
    total = 0
    for root in roots:
        for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else ():
            if "subagents" in path.parts:
                continue  # a subagent's transcript, not a conversation
            try:
                n = import_file(conn, path)
            except Exception as e:
                log(f"  skipped {path}: {type(e).__name__}: {e}")
                continue
            total += n
            if n:
                log(f"  {n:6d}  {path}")
    return total
