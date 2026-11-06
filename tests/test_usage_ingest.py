"""``usage.ingest``: one row per API message, reading only new bytes."""

import json

import pytest

from cabildo import usage


def line(message_id="m1", output=10, model="claude-x", **extra):
    return (
        json.dumps(
            {
                "timestamp": "2026-11-06T10:00:00Z",
                "sessionId": "parent",
                "message": {
                    "id": message_id,
                    "model": model,
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": output,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 4,
                    },
                },
                **extra,
            }
        )
        + "\n"
    )


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "PROJECTS", tmp_path)
    (tmp_path / "proj" / "parent" / "subagents").mkdir(parents=True)
    return tmp_path


def rows(conn):
    return conn.execute("SELECT * FROM usage_messages ORDER BY message_id").fetchall()


def test_repeated_lines_of_a_message_count_once_with_the_largest_output(root, conn):
    (root / "proj" / "parent.jsonl").write_text(line(output=2) + line(output=9) + line(output=9))
    usage.ingest(conn)
    assert [r["output"] for r in rows(conn)] == [9]


def test_a_message_repeated_in_another_file_counts_once(root, conn):
    (root / "proj" / "parent.jsonl").write_text(line())
    (root / "proj" / "resumed.jsonl").write_text(line())
    usage.ingest(conn)
    assert len(rows(conn)) == 1


def test_a_subagent_message_belongs_to_the_parent_session_and_project(root, conn):
    path = root / "proj" / "parent" / "subagents" / "agent-a.jsonl"
    path.write_text(line(agentId="a"))
    usage.ingest(conn)
    row = rows(conn)[0]
    assert (row["session_id"], row["agent_id"], row["project"]) == ("parent", "a", "proj")


def test_synthetic_and_broken_lines_are_skipped(root, conn):
    (root / "proj" / "parent.jsonl").write_text(
        line(model="<synthetic>") + '{"usage": broken\n' + line("m2")
    )
    usage.ingest(conn)
    assert [r["message_id"] for r in rows(conn)] == ["m2"]


def test_a_later_ingest_reads_only_the_new_bytes(root, conn):
    path = root / "proj" / "parent.jsonl"
    path.write_text(line("m1"))
    usage.ingest(conn)
    conn.execute("DELETE FROM usage_messages")
    with path.open("a") as fh:
        fh.write(line("m2"))
    usage.ingest(conn)
    assert [r["message_id"] for r in rows(conn)] == ["m2"]


def test_a_line_without_its_newline_waits_for_the_next_ingest(root, conn):
    path = root / "proj" / "parent.jsonl"
    whole = line("m2")
    path.write_text(line("m1") + whole[:10])
    usage.ingest(conn)
    assert [r["message_id"] for r in rows(conn)] == ["m1"]
    with path.open("a") as fh:
        fh.write(whole[10:])
    usage.ingest(conn)
    assert [r["message_id"] for r in rows(conn)] == ["m1", "m2"]


def test_a_truncated_file_is_read_again(root, conn):
    path = root / "proj" / "parent.jsonl"
    path.write_text(line("m1") + line("m2"))
    usage.ingest(conn)
    path.write_text(line("m3"))
    usage.ingest(conn)
    assert "m3" in [r["message_id"] for r in rows(conn)]
