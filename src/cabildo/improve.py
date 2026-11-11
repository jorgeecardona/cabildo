"""Proposals: changes to the setup, written from friction and checked by a reviewer.

``propose`` gives the recent friction to a strong model that can read the setup files
(skills, CLAUDE.md and AGENTS.md files, hooks, cabildo's source). It finds the recurring
patterns and writes one concrete change per pattern, citing the friction rows as evidence.
``review`` asks a second model to argue against each proposal; only the ones it approves
reach the human (step 4, the Approve / Reject popup).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PROPOSER = "opus"
REVIEWER = "fable"
READ_TOOLS = "Read,Grep,Glob"
HOME = Path.home()
SETUP = (
    HOME / ".claude",  # CLAUDE.md, skills, agents, output styles, settings, hooks
    HOME / "stuff/projects",  # AGENTS.md files and every project, cabildo included
)

PROPOSE_PROMPT = """You improve a developer's AI coding setup from evidence.

The setup: Claude Code configured in ~/.claude (CLAUDE.md, skills/, agents/, output-styles/,
settings.json, hooks), the AGENTS.md and CLAUDE.md files under ~/stuff/projects, and cabildo
(~/stuff/projects/oss/cabildo), the tool that coordinates the agents. Read those files.

Below is FRICTION: numbered moments where the developer corrected, repeated, or complained.
Find the patterns that recur (2+ rows) or cost the most. For each pattern, propose ONE
concrete change to the setup that would have prevented it: name the file, and say exactly
what to add or change. Prefer small changes to a skill, an instruction file, or a hook over
new features. Skip a pattern already handled by the setup or by an open proposal below.
At most {top} proposals, best first. evidence = the friction numbers it rests on.

Open proposals (do not repeat):
{open}

Friction:
{rows}"""

REVIEW_PROMPT = """You are the reviewer. Argue against this proposed change to a developer's
AI setup. You may read the files. Reject it if the evidence does not show a real recurring
problem, if the setup already handles it, if the change is vague, or if it would cause new
friction. Otherwise approve it.

Proposal:
{proposal}

Evidence (the developer's words and what went wrong):
{evidence}"""

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "pattern": {"type": "string"},
                    "target": {"type": "string"},
                    "change": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["title", "pattern", "target", "change", "evidence"],
            },
        }
    },
    "required": ["proposals"],
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "reasons": {"type": "string"},
    },
    "required": ["verdict", "reasons"],
}


@dataclass(frozen=True)
class Row:
    """One friction row as the proposer sees it."""

    key: tuple[str, str]  # (session_id, uuid)
    project: str
    area: str
    summary: str
    quote: str


def ask(prompt: str, model: str, schema: dict, *, run=subprocess.run) -> dict:
    """One ``claude -p`` call that may read files and must answer in ``schema``."""
    p = run(
        [
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--no-session-persistence",
            "--allowedTools",
            READ_TOOLS,
            "--tools",
            READ_TOOLS,
            *(a for d in SETUP for a in ("--add-dir", str(d))),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=str(HOME),
    )
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.startswith("{")]
    if not lines:
        raise RuntimeError(f"{model} gave no JSON (exit {p.returncode}): {p.stderr[-300:]}")
    out = json.loads(lines[-1]).get("structured_output")
    if not isinstance(out, dict):
        raise RuntimeError(f"{model} gave no structured output")
    return out


def friction_rows(conn, since: float) -> list[Row]:
    rows = conn.execute(
        "SELECT session_id, uuid, coalesce(project_name, '?'), area, summary, quote "
        "FROM friction WHERE ts > ? ORDER BY ts",
        (since,),
    ).fetchall()
    return [Row((r[0], r[1]), r[2], r[3], r[4], r[5]) for r in rows]


def open_titles(conn) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT title FROM proposals WHERE status IN ('proposed', 'approved')"
        )
    ]


def propose(conn, *, since: float, top: int = 5, run=subprocess.run) -> list[int]:
    """Write proposals from friction since ``since``. Returns the new proposal ids."""
    rows = friction_rows(conn, since)
    if not rows:
        return []
    listing = "\n".join(
        f"[{i}] ({r.project}, {r.area}) {r.summary} | said: {r.quote[:160]!r}"
        for i, r in enumerate(rows)
    )
    prompt = PROPOSE_PROMPT.format(
        top=top, open="\n".join(f"- {t}" for t in open_titles(conn)) or "(none)", rows=listing
    )
    out = ask(prompt, PROPOSER, PROPOSAL_SCHEMA, run=run)
    ids = []
    for p in out.get("proposals", [])[:top]:
        keys = [
            rows[i].key for i in p.get("evidence", []) if isinstance(i, int) and 0 <= i < len(rows)
        ]
        if not keys:
            continue  # a proposal with no evidence is an opinion
        cur = conn.execute(
            "INSERT INTO proposals (created_at, title, pattern, target, change, evidence) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), p["title"], p["pattern"], p["target"], p["change"], json.dumps(keys)),
        )
        ids.append(int(cur.lastrowid or 0))
    conn.commit()
    return ids


def review(conn, pid: int, *, run=subprocess.run) -> str:
    """Have the reviewer argue against proposal ``pid``. Returns its new status."""
    p = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    keys = json.loads(p["evidence"])
    evidence = "\n".join(
        f"- {r[0]} | said: {r[1]!r}"
        for k in keys
        for r in conn.execute(
            "SELECT summary, quote FROM friction WHERE session_id = ? AND uuid = ?", tuple(k)
        )
    )
    body = (
        f"title: {p['title']}\npattern: {p['pattern']}\ntarget: {p['target']}\n"
        f"change: {p['change']}"
    )
    out = ask(
        REVIEW_PROMPT.format(proposal=body, evidence=evidence), REVIEWER, VERDICT_SCHEMA, run=run
    )
    status = "proposed" if out.get("verdict") == "approve" else "rejected-by-review"
    conn.execute(
        "UPDATE proposals SET review = ?, status = ? WHERE id = ?",
        (f"{out.get('verdict')}: {out.get('reasons', '')}", status, pid),
    )
    conn.commit()
    return status
