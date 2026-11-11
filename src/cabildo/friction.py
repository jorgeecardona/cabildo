"""Friction: the turns where the human corrected, repeated, or complained about the tools.

``scan`` reads new user turns from ``transcript_messages``, asks a small model which ones are
friction with the setup (Claude Code, cabildo, skills, hooks, tools), and stores those in the
``friction`` table. It never stores personal content: a turn about the human's life, health,
or work outside the tools is not friction. ``report`` groups the rows by area.

This is step 1 of the self-improvement loop: the evidence the proposal step works from.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

KINDS = ("correction", "repeat", "broken", "missing", "slow", "other")
MODEL = "haiku"
BATCH = 40
MAX_BODY = 1500  # longer user turns are pasted material, not a reaction

PROMPT = """You review turns a developer typed to AI coding agents. For each numbered turn,
decide if it shows FRICTION with the developer's tool setup: Claude Code, its agents, the
cabildo coordination tool, skills, hooks, browsers, CLIs, CI. Friction means: the developer
corrects the agent, repeats a request, says something is broken or still broken, asks for a
capability the tools lack, or is annoyed at slowness or extra manual steps.

NOT friction: normal requests, questions, "continue", "please", thanks, and anything personal
(life, health, feelings, jobs, family). Never describe personal content.

For each friction turn give: i (its number), kind (one of {kinds}), area (the tool or part
it is about, 2-4 words, lowercase), summary (one line, what went wrong, in tool terms).
Omit turns that are not friction.

The agent's previous reply is shown before each turn, clipped, as context.

{turns}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "area": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["i", "kind", "area", "summary"],
            },
        }
    },
    "required": ["items"],
}


@dataclass(frozen=True)
class Turn:
    """One user turn to classify, with the agent reply before it."""

    session_id: str
    uuid: str
    ts: float
    project_name: str | None
    body: str
    before: str


@dataclass(frozen=True)
class Friction:
    """One classified friction turn."""

    turn: Turn
    kind: str
    area: str
    summary: str


def pending(conn, since: float, limit: int) -> list[Turn]:
    """User turns after the scan cursor (or ``since``, whichever is later), oldest first."""
    row = conn.execute("SELECT ts FROM friction_cursor WHERE id = 1").fetchone()
    start = max(row[0] if row else 0.0, since)
    rows = conn.execute(
        """SELECT u.session_id, u.uuid, u.ts, u.project_name, u.body,
                  (SELECT a.body FROM transcript_messages a
                    WHERE a.session_id = u.session_id AND a.role = 'assistant' AND a.ts < u.ts
                    ORDER BY a.ts DESC LIMIT 1) AS before
           FROM transcript_messages u
           WHERE u.role = 'user' AND u.ts > ? AND length(u.body) <= ?
             AND u.body NOT LIKE 'The cabildo-push plugin sent%'
           ORDER BY u.ts LIMIT ?""",
        (start, MAX_BODY, limit),
    ).fetchall()
    return [Turn(r[0], r[1], r[2] or 0.0, r[3], r[4], (r[5] or "")[-300:]) for r in rows]


def prompt_for(turns: list[Turn]) -> str:
    """The classification prompt for one batch."""
    text = "\n\n".join(
        f"[{i}] agent before: {t.before!r}\n[{i}] developer: {t.body!r}"
        for i, t in enumerate(turns)
    )
    return PROMPT.format(kinds=", ".join(KINDS), turns=text)


def classify(turns: list[Turn], *, run=subprocess.run) -> list[Friction]:
    """Ask the model which turns are friction. Raises ``RuntimeError`` when the call fails."""
    p = run(
        [
            "claude",
            "-p",
            prompt_for(turns),
            "--model",
            MODEL,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SCHEMA),
            "--no-session-persistence",
            "--tools",
            "",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.startswith("{")]
    if not lines:
        raise RuntimeError(f"classifier gave no JSON (exit {p.returncode}): {p.stderr[-300:]}")
    out = json.loads(lines[-1]).get("structured_output") or {}
    found = []
    for item in out.get("items", []):
        i = item.get("i")
        if isinstance(i, int) and 0 <= i < len(turns) and item.get("kind") in KINDS:
            found.append(
                Friction(
                    turns[i],
                    item["kind"],
                    str(item["area"]).strip().lower(),
                    str(item["summary"]).strip(),
                )
            )
    return found


def save(conn, found: list[Friction], last_ts: float) -> None:
    """Store the friction rows and move the cursor past the whole batch."""
    conn.executemany(
        "INSERT OR REPLACE INTO friction VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                f.turn.session_id,
                f.turn.uuid,
                f.turn.ts,
                f.turn.project_name,
                f.kind,
                f.area,
                f.summary,
                f.turn.body[:300],
            )
            for f in found
        ],
    )
    conn.execute("INSERT OR REPLACE INTO friction_cursor VALUES (1, ?)", (last_ts,))
    conn.commit()


def scan(conn, *, since: float = 0.0, limit: int = 2000, log=print, run=subprocess.run) -> int:
    """Classify up to ``limit`` new user turns, ``BATCH`` per model call. Returns rows saved."""
    total = 0
    while limit > 0:
        turns = pending(conn, since, min(BATCH, limit))
        if not turns:
            break
        found = classify(turns, run=run)
        save(conn, found, turns[-1].ts)
        total += len(found)
        limit -= len(turns)
        log(f"  {len(turns)} turns, {len(found)} friction")
    return total


def report(conn, *, since: float = 0.0, top: int = 15) -> str:
    """Friction grouped by area, most frequent first, with the newest summaries."""
    rows = conn.execute(
        "SELECT area, count(*) n FROM friction WHERE ts > ? GROUP BY area ORDER BY n DESC LIMIT ?",
        (since, top),
    ).fetchall()
    out = []
    for area, n in rows:
        out.append(f"{n:4d}  {area}")
        for (summary,) in conn.execute(
            "SELECT summary FROM friction WHERE area = ? AND ts > ? ORDER BY ts DESC LIMIT 3",
            (area, since),
        ):
            out.append(f"        - {summary}")
    return "\n".join(out) or "no friction recorded"
