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
