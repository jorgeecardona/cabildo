"""Which of the app's Claudes may be stopped for being idle (``[workspace].stop_idle_hours``).

A Claude may be stopped when all of these hold:

- Claude Code reports it ``idle`` (not busy, not waiting for an answer) since the limit;
- the transcripts of its subagents and workflows were not written since the limit;
- it runs no shell command (a background command or a monitor);
- it has no scheduled cron or wakeup in cabildo.

The conversation's own transcript is not looked at: Claude Code appends summaries and
records to it while it is idle.

Anything that cannot be read counts as a reason not to stop. The app adds one rule of its
own: a conversation the person looked at since the limit is not asked about.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from cabildo import crons, store
from cabildo.workspace.sessions import config_dir

# Claude Code runs every shell command through its snapshot of the user's shell; an MCP
# server is started without it.
SHELL_MARK = "/shell-snapshots/"


@dataclass(frozen=True)
class Candidate:
    """One pane's Claude."""

    session_id: str  # what the pane shows
    pid: int  # that Claude's process
    ids: tuple[str, ...]  # every session of the conversation
    transcript: Path  # of the conversation's newest session; its subagents are beside it


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    command: str


def idle_since(candidate: Candidate) -> float | None:
    """The time (epoch seconds) of the last activity of an idle Claude; None when it is
    not idle, or that is not known.
    """
    try:
        path = config_dir() / "sessions" / f"{candidate.pid}.json"
        found = json.loads(path.read_text(encoding="utf-8"))
        if (
            found["sessionId"] != candidate.session_id
            or found["kind"] != "interactive"
            or found["status"] != "idle"
        ):
            return None
        since = float(found["statusUpdatedAt"]) / 1000
        subagents = candidate.transcript.with_suffix("") / "subagents"
        return max([since, *(f.stat().st_mtime for f in subagents.rglob("*.jsonl"))])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def processes() -> tuple[Process, ...] | None:
    """Every process of the machine (``ps``, Linux and macOS); None when it fails."""
    try:
        r = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=", "-o", "ppid=", "-o", "command="],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            stdin=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = []
    for line in r.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3:
            out.append(Process(int(fields[0]), int(fields[1]), fields[2]))
    return tuple(out)


def runs_shell(pid: int, found: Iterable[Process]) -> bool:
    """Whether a process under ``pid`` is a shell command Claude started."""
    under: dict[int, list[Process]] = {}
    for p in found:
        under.setdefault(p.ppid, []).append(p)
    todo = list(under.get(pid, ()))
    while todo:
        p = todo.pop()
        if SHELL_MARK in p.command:
            return True
        todo += under.get(p.pid, ())
    return False


def scheduled(ids: tuple[str, ...]) -> bool:
    """Whether cabildo holds a cron or a wakeup for one of these sessions; True when the
    bus cannot be read.
    """
    try:
        conn = store.connect()
        try:
            return any(crons.list_jobs(conn, sid) for sid in ids)
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return True


def stoppable(
    candidates: Iterable[Candidate],
    before: float,
    *,
    list_processes: Callable[[], tuple[Process, ...] | None] = processes,
    has_schedule: Callable[[tuple[str, ...]], bool] = scheduled,
) -> tuple[Candidate, ...]:
    """The candidates idle since ``before`` (epoch seconds) or longer that run no shell
    command and have nothing scheduled.
    """
    idle = [c for c in candidates if (at := idle_since(c)) is not None and at <= before]
    if not idle:
        return ()
    found = list_processes()
    if found is None:
        return ()
    return tuple(c for c in idle if not runs_shell(c.pid, found) and not has_schedule(c.ids))
