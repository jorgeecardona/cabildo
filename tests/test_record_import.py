"""``cabildo messages --import``: record whole transcript files, also trashed ones."""

from __future__ import annotations

import json
from pathlib import Path

from cabildo import record, store


def _line(uuid: str, role: str, text: str) -> str:
    content: object = text if role == "user" else [{"type": "text", "text": text}]
    entry = {
        "type": role,
        "uuid": uuid,
        "cwd": "/nonexistent/repo",
        "timestamp": "2026-11-10T10:00:00.000Z",
        "message": {"role": role, "content": content},
    }
    # compact: the reader prefilters on the substring '"type":"user"'
    return json.dumps(entry, separators=(",", ":")) + "\n"


def test_import_all_records_every_turn_once_and_skips_subagents(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path / "home"))
    trash = tmp_path / "trash" / "20261110-aaaa"
    trash.mkdir(parents=True)
    (trash / "aaaa.jsonl").write_text(
        "".join(_line(f"u{i}", "user" if i % 2 else "assistant", f"t{i}") for i in range(1200))
    )
    sub = tmp_path / "trash" / "x" / "subagents"
    sub.mkdir(parents=True)
    (sub / "bbbb.jsonl").write_text(_line("s1", "user", "sub"))

    with store.connect() as conn:
        first = record.import_all(conn, (tmp_path / "trash", tmp_path / "missing"))
        again = record.import_all(conn, (tmp_path / "trash",))
        sessions = {r[0] for r in conn.execute("SELECT session_id FROM transcript_messages")}
    assert first == 1200  # more than one max_turns batch
    assert again == 0
    assert sessions == {"aaaa"}
