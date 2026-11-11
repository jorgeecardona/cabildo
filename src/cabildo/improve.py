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


def ask(
    prompt: str, model: str, schema: dict, *, run=subprocess.run, tools: str = READ_TOOLS
) -> dict:
    """One ``claude -p`` call that may read files and must answer in ``schema``."""
    p = run(
        [
            "claude",
            "-p",
            "--model",
            model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--no-session-persistence",
            "--allowedTools",
            tools,
            "--tools",
            tools,
            *(a for d in SETUP for a in ("--add-dir", str(d))),
        ],
        input=prompt,
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


APPLY_TOOLS = "Read,Grep,Glob,Edit"
APPLY_DIRS = (HOME / ".claude", HOME / "stuff/projects")

APPLY_PROMPT = """The developer approved this change to their AI setup. Apply it now.

Edit only instruction files: ~/.claude/CLAUDE.md, ~/.claude/skills/, ~/.claude/agents/,
~/.claude/output-styles/, and AGENTS.md or CLAUDE.md files under ~/stuff/projects. Keep each
edit small and in the file's own style. If the change needs source code, a hook script, or
settings.json, do not edit it: answer applied=false and say what a person must do.
Then answer with what you changed, one line per file.

Reviewer notes to follow while applying:
{review}

Proposal:
title: {title}
target: {target}
change: {change}"""

APPLY_SCHEMA = {
    "type": "object",
    "properties": {"applied": {"type": "boolean"}, "summary": {"type": "string"}},
    "required": ["applied", "summary"],
}

APPROVE, REJECT = "Approve", "Reject"


def daily(conn, *, days: float = 7.0, top: int = 3, log=print, run=subprocess.run) -> list[int]:
    """The daily pass: scan new friction, propose from the last ``days``, review each.

    Returns the ids the reviewer approved; the daemon pops those to the human.
    """
    from . import friction

    friction.scan(conn, since=time.time() - days * 86400, log=log, run=run)
    approved = []
    for pid in propose(conn, since=time.time() - days * 86400, top=top, run=run):
        status = review(conn, pid, run=run)
        log(f"  #{pid}: {status}")
        if status == "proposed":
            approved.append(pid)
    return approved


def waiting(conn) -> list[dict]:
    """Proposals the reviewer approved that wait for the human."""
    rows = conn.execute(
        "SELECT * FROM proposals WHERE status = 'proposed' AND review IS NOT NULL ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def decide(conn, pid: int, choice: str | None, *, run=subprocess.run) -> str | None:
    """Apply a popup choice. Approve runs the apply agent. Returns the new status, or None.

    Does nothing when the popup was dismissed or the proposal was already decided.
    """
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    if row is None or row["status"] != "proposed" or choice not in (APPROVE, REJECT):
        return None
    if choice == REJECT:
        status, note = "rejected", "rejected by you"
    else:
        conn.execute("UPDATE proposals SET status = 'approved' WHERE id = ?", (pid,))
        conn.commit()
        prompt = APPLY_PROMPT.format(
            review=row["review"] or "",
            title=row["title"],
            target=row["target"],
            change=row["change"],
        )
        try:
            out = ask(prompt, PROPOSER, APPLY_SCHEMA, run=run, tools=APPLY_TOOLS)
            status = "applied" if out.get("applied") else "needs-you"
            note = str(out.get("summary", ""))
        except RuntimeError as e:
            status, note = "apply-failed", str(e)
    conn.execute(
        "UPDATE proposals SET status = ?, review = coalesce(review, '') || ? WHERE id = ?",
        (status, f"\n\n[{status}] {note}", pid),
    )
    from . import store

    store.post_message(
        conn,
        from_session=store.HUMAN_SESSION,
        from_handle="improve",
        body=f"proposal #{pid} ({row['title']}) → {status}: {note[:400]}",
        to_session=store.HUMAN_SESSION,
        urgent=False,
    )
    conn.commit()
    return status


def popup(p: dict) -> tuple[str, str]:
    """The popup title and body for one proposal."""
    return f"Improve: {p['title']}", f"{p['pattern']}\n\nChange: {p['change'][:400]}\n\n#{p['id']}"


def prompt_waiting(conn, shown: set[int]) -> int:
    """Pop Approve / Reject for each waiting proposal not in ``shown``, on threads.

    ``shown`` lives in the daemon, so a restart shows the waiting ones again. Returns how many
    popups it started.
    """
    import threading

    from . import desktop, store

    new = [p for p in waiting(conn) if p["id"] not in shown]
    for p in new:
        shown.add(p["id"])

        def run(p: dict = p) -> None:
            choice = desktop.ask(*popup(p), [APPROVE, REJECT])
            own = store.connect()
            try:
                decide(own, p["id"], choice)
            finally:
                own.close()

        threading.Thread(target=run, daemon=True).start()
    return len(new)
