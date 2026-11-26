"""``cabildo stats``: the mode line's numbers as data."""

from __future__ import annotations

import json
import time

import pytest

from cabildo import cli, stats, store, usage
from cabildo.workspace import plan, sessions

from .test_spend import line


@pytest.fixture
def claude(home, monkeypatch):
    """A Claude config dir with one transcript, and no network, gateway or runner."""
    root = home / "claude"
    project = root / "projects" / "-repo"
    project.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.setattr(usage, "PROJECTS", root / "projects")
    monkeypatch.setattr(plan, "shared", lambda not_before: None)
    monkeypatch.setattr(plan, "auth_kind", lambda: plan.SUBSCRIPTION)
    monkeypatch.setattr(stats.config, "gateway_table", dict)
    monkeypatch.setattr(stats.jupyter, "statuses", list)
    return project


def reply(message_id: str, *, input_tokens: int = 0, output: int = 0, cache_read: int = 0) -> str:
    """A transcript line as ``_reply_tokens`` reads it."""
    when = "2026-01-01T00:00:00.000Z"
    text = line(message_id, when, input_tokens=input_tokens, output=output, cache_read=cache_read)
    raw = json.loads(text)
    raw["message"] = {"role": "assistant", **raw["message"]}
    raw["message"]["id"] = f"msg_{message_id}"
    return json.dumps(raw)


def test_a_session_row_shows_how_full_its_context_is(claude, conn):
    store.register_session(conn, "s", cwd="/repo")
    conn.commit()
    claude.joinpath("s.jsonl").write_text(
        reply("one", input_tokens=10, output=5)
        + "\n"
        + reply("two", input_tokens=1_000, cache_read=799_000, output=2_000)
        + "\n"
    )

    found = {g.key: g for g in stats.collect("/repo")}

    (row,) = found["sessions"].rows
    assert row.value.startswith("opus 5.5 · ctx 80%")
    assert row.level == "warn"
    assert "context: 800k of 1.0M (80%)" in row.detail
    assert "estimated cost" not in row.detail  # a subscription: no spend row, so no cost
    assert "spend" not in found


def test_the_last_reply_is_found_past_a_long_tail(claude):
    path = claude / "s.jsonl"
    filler = json.dumps({"type": "user", "text": "x" * 1000})
    path.write_text(reply("one", input_tokens=7) + "\n" + (filler + "\n") * 600)

    found = sessions.last_reply(path)

    assert found is not None
    assert found.context == 7


def test_a_failed_intent_is_an_alert_with_its_reason(claude, conn):
    iid = store.request_intent(
        conn,
        from_session="s",
        from_handle=None,
        project="/repo",
        action="push",
        params={"cwd": "/repo"},
    )
    conn.execute(
        "UPDATE intents SET status='failed', finished_at=?, result=? WHERE id=?",
        (time.time(), json.dumps({"reason": "the gate failed"}), iid),
    )
    conn.commit()

    (group,) = [g for g in stats.collect("/repo") if g.key == "intents"]

    assert group.rows[0].level == "alert"
    assert "the gate failed" in group.rows[0].detail


def test_the_plan_group_shows_both_limits(claude):
    usage_now = plan.PlanUsage("max", plan.Limit(95, 0), plan.Limit(12, 0))
    group = stats.plan_group(plan.Shared(usage_now, plan.PlanPace(None, None), 0))

    assert group.title == "max plan"
    assert [(r.label, r.value, r.level) for r in group.rows] == [
        ("5 hours", "95% used", "alert"),
        ("7 days", "12% used", ""),
    ]


def test_json_output_is_the_groups(claude, conn, capsys):
    store.register_session(conn, "s", cwd="/repo")
    conn.commit()

    assert cli.main(["stats", "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["groups"][0]["key"] == "sessions"
    assert printed["groups"][0]["rows"][0]["value"] == "no reply yet"
