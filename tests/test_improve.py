"""``cabildo improve``: proposals need evidence, and the reviewer decides which survive."""

from __future__ import annotations

import json
from types import SimpleNamespace

from cabildo import improve, store


def _reply(out: dict):
    return SimpleNamespace(stdout=json.dumps({"structured_output": out}), stderr="", returncode=0)


def test_proposals_keep_evidence_and_the_reviewer_can_reject(home) -> None:
    conn = store.connect()
    conn.execute(
        "INSERT INTO friction VALUES ('s1','u1',10,'cabildo','missing','grants','no button','cli?')"
    )
    conn.commit()
    answers = [
        {
            "proposals": [
                {"title": "Buttons", "pattern": "p", "target": "t", "change": "c", "evidence": [0]},
                {"title": "Opinion", "pattern": "p", "target": "t", "change": "c", "evidence": []},
            ]
        },
        {"verdict": "reject", "reasons": "already handled"},
    ]
    run = lambda *_a, **_k: _reply(answers.pop(0))
    ids = improve.propose(conn, since=0, run=run)
    assert len(ids) == 1  # the proposal with no evidence is dropped
    assert improve.review(conn, ids[0], run=run) == "rejected-by-review"
    row = conn.execute("SELECT * FROM proposals").fetchone()
    assert json.loads(row["evidence"]) == [["s1", "u1"]]
    assert row["review"] == "reject: already handled"


def _proposal(conn, review: str | None = "approve: ok") -> int:
    cur = conn.execute(
        "INSERT INTO proposals (created_at, title, pattern, target, change, evidence, review) "
        "VALUES (1, 'T', 'P', '~/.claude/CLAUDE.md', 'C', '[]', ?)",
        (review,),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def test_only_reviewed_proposals_wait_for_the_human(home) -> None:
    conn = store.connect()
    reviewed = _proposal(conn)
    _proposal(conn, review=None)
    assert [p["id"] for p in improve.waiting(conn)] == [reviewed]


def test_approve_runs_the_apply_agent_with_edit(home) -> None:
    conn = store.connect()
    pid = _proposal(conn)
    calls: list[list[str]] = []

    def run(args, **_k):
        calls.append(args)
        return _reply({"applied": True, "summary": "CLAUDE.md: added one rule"})

    assert improve.decide(conn, pid, improve.APPROVE, run=run) == "applied"
    assert "Edit" in calls[0][calls[0].index("--tools") + 1]
    assert improve.decide(conn, pid, improve.APPROVE, run=run) is None  # decided once
    assert len(calls) == 1


def test_reject_and_dismiss(home) -> None:
    conn = store.connect()
    pid = _proposal(conn)
    assert improve.decide(conn, pid, None) is None  # dismissed: still waiting
    assert improve.waiting(conn)
    assert improve.decide(conn, pid, improve.REJECT) == "rejected"
    assert improve.waiting(conn) == []
