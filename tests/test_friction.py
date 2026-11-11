"""``cabildo friction``: classify user turns, store only friction, report by area."""

from __future__ import annotations

import json
from types import SimpleNamespace

from cabildo import friction, store


def _turn(conn, uuid: str, ts: float, role: str, body: str) -> None:
    conn.execute(
        "INSERT INTO transcript_messages (session_id, uuid, ts, role, project_name, body) "
        "VALUES ('s1', ?, ?, ?, 'cabildo', ?)",
        (uuid, ts, role, body),
    )


def test_scan_saves_friction_and_moves_the_cursor(home) -> None:
    conn = store.connect()
    _turn(conn, "a1", 1.0, "assistant", "Run `cabildo grant 9`.")
    _turn(conn, "u1", 2.0, "user", "I dont want to use the cli for that")
    _turn(conn, "u2", 3.0, "user", "please")
    conn.commit()
    prompts: list[str] = []

    def run(args, **_k):
        prompts.append(_k["input"])
        out = {"items": [{"i": 0, "kind": "missing", "area": "Grants", "summary": "no button"}]}
        return SimpleNamespace(
            stdout=json.dumps({"structured_output": out}), stderr="", returncode=0
        )

    assert friction.scan(conn, log=lambda _m: None, run=run) == 1
    assert "Run `cabildo grant 9`." in prompts[0]  # the agent reply is the context
    rows = conn.execute("SELECT area, kind FROM friction").fetchall()
    assert [tuple(r) for r in rows] == [("grants", "missing")]
    assert friction.pending(conn, 0, 10) == []  # the cursor is past both turns
    assert "grants" in friction.report(conn)
