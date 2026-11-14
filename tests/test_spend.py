"""``usage.spend``: the transcripts' token counts, priced, over today / 7 days / all time."""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from cabildo import prices, usage
from cabildo.workspace import plan, spend

DAY = 86400.0


def at(now: float, ago: float) -> str:
    """An ISO timestamp ``ago`` seconds before ``now``, in the shape Claude Code writes."""
    return dt.datetime.fromtimestamp(now - ago, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def line(
    message_id: str,
    when: str,
    *,
    model: str = "claude-opus-5-5",
    input_tokens: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    cache_write_1h: int = 0,
    speed: str | None = None,
) -> str:
    usage_block: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "cache_creation": {
            "ephemeral_1h_input_tokens": cache_write_1h,
            "ephemeral_5m_input_tokens": cache_write - cache_write_1h,
        },
    }
    if speed:
        usage_block["speed"] = speed
    return (
        json.dumps(
            {
                "timestamp": when,
                "sessionId": "s",
                "message": {"id": message_id, "model": model, "usage": usage_block},
            }
        )
        + "\n"
    )


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "PROJECTS", tmp_path)
    (tmp_path / "proj").mkdir()
    return tmp_path


# --------------------------------------------------------------------------- #
# Pricing a window
# --------------------------------------------------------------------------- #
def test_each_kind_of_token_is_priced_at_its_own_rate(root, conn):
    now = 1_800_000_000.0
    (root / "proj" / "a.jsonl").write_text(
        line(
            "m1",
            at(now, 60),
            input_tokens=1_000_000,
            output=1_000_000,
            cache_read=1_000_000,
            cache_write=2_000_000,
            cache_write_1h=1_000_000,  # half the writes are the dearer hour-long kind
        )
    )
    usage.ingest(conn)
    w = usage.spend(conn, now).all_time
    p = prices.PRICES["claude-opus-5-5"]
    assert w.cost == pytest.approx(
        p.input + p.output + p.cache_read + p.cache_write_5m + p.cache_write_1h
    )
    assert w.tokens == 5_000_000
    assert w.unpriced == 0


def test_grouping_before_pricing_matches_pricing_each_message(root, conn):
    """The aggregate groups rows by model, speed and tier, then prices the group. That is
    only sound because the rates are per token: this pins it against the per-row sum.
    """
    now = 1_800_000_000.0
    text = "".join(
        line(
            f"m{i}",
            at(now, 60 * i),
            model=model,
            input_tokens=100 * i,
            output=10 * i,
            cache_read=1000 * i,
            cache_write=50 * i,
        )
        for i, model in enumerate(["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5"] * 4, 1)
    )
    (root / "proj" / "a.jsonl").write_text(text)
    usage.ingest(conn)
    per_row = sum(
        prices.cost(
            r["model"],
            input_tokens=r["input"],
            output_tokens=r["output"],
            cache_write_5m=r["cache_write"] - r["cache_write_1h"],
            cache_write_1h=r["cache_write_1h"],
            cache_read=r["cache_read"],
        )
        or 0.0
        for r in conn.execute("SELECT * FROM usage_messages")
    )
    assert usage.spend(conn, now).all_time.cost == pytest.approx(per_row)


def test_an_unknown_model_is_counted_in_tokens_and_left_out_of_the_cost(root, conn):
    now = 1_800_000_000.0
    (root / "proj" / "a.jsonl").write_text(
        line("known", at(now, 60), input_tokens=1_000_000)
        + line("later", at(now, 60), model="claude-opus-9", input_tokens=500_000)
    )
    usage.ingest(conn)
    w = usage.spend(conn, now).all_time
    assert w.cost == pytest.approx(prices.PRICES["claude-opus-5-5"].input)
    assert w.tokens == 1_500_000
    assert w.unpriced == 500_000  # named, not folded into the dollar figure


def test_a_fast_mode_reply_is_unpriced(root, conn):
    now = 1_800_000_000.0
    (root / "proj" / "a.jsonl").write_text(
        line("m1", at(now, 60), input_tokens=1_000_000, speed="fast")
    )
    usage.ingest(conn)
    w = usage.spend(conn, now).all_time
    assert (w.cost, w.unpriced, w.tokens) == (0.0, 1_000_000, 1_000_000)


def test_a_long_haiku_prompt_is_priced_in_the_upper_tier(root, conn):
    now = 1_800_000_000.0
    over = prices.LONG_PROMPT_TOKENS + 1
    (root / "proj" / "a.jsonl").write_text(
        line("m1", at(now, 60), model=prices.LONG_PROMPT_MODEL, input_tokens=over)
    )
    usage.ingest(conn)
    cost = usage.spend(conn, now).all_time.cost
    assert cost == pytest.approx(over * prices.LONG_PROMPT_PRICE.input / prices.PER_MILLION)
    assert cost > over * prices.PRICES[prices.LONG_PROMPT_MODEL].input / prices.PER_MILLION


# --------------------------------------------------------------------------- #
# The three windows
# --------------------------------------------------------------------------- #
def test_the_windows_each_cover_their_own_period(root, conn):
    now = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC).timestamp()
    (root / "proj" / "a.jsonl").write_text(
        line("now", at(now, 60), output=1000)
        + line("yesterday", at(now, 2 * DAY), output=100)
        + line("old", at(now, 30 * DAY), output=10)
    )
    usage.ingest(conn)
    s = usage.spend(conn, now)
    assert (s.today.output, s.week.output, s.all_time.output) == (1000, 1100, 1110)


def test_today_starts_at_local_midnight_not_24_hours_back(root, conn):
    """A figure labelled "today" that dropped this morning's work at noon would be wrong
    in the way nobody checks, so the cut is the calendar's, not a rolling day's.
    """
    # mktime reads a local wall clock, DST and all, which is exactly what "today" means
    noon = time.mktime((2026, 11, 14, 12, 0, 0, 0, 0, -1))
    before_midnight = time.mktime((2026, 11, 13, 23, 0, 0, 0, 0, -1))
    after_midnight = time.mktime((2026, 11, 14, 1, 0, 0, 0, 0, -1))
    (root / "proj" / "a.jsonl").write_text(
        line("late", at(noon, noon - before_midnight), output=7)
        + line("early", at(noon, noon - after_midnight), output=11)
    )
    usage.ingest(conn)
    assert usage.spend(conn, noon).today.output == 11  # only the one after midnight


def test_an_empty_table_is_zero_everywhere_not_an_error(conn):
    s = usage.spend(conn, 1_800_000_000.0)
    assert (s.today.tokens, s.week.cost, s.all_time.unpriced) == (0, 0.0, 0)


# --------------------------------------------------------------------------- #
# Which login the mode line is for
# --------------------------------------------------------------------------- #
def test_oauth_credentials_read_as_a_subscription(monkeypatch):
    monkeypatch.setattr(plan, "_credentials", lambda: json.dumps({"claudeAiOauth": {"a": 1}}))
    assert plan.auth_kind() == plan.SUBSCRIPTION


def test_credentials_without_an_oauth_entry_read_as_an_api_key(monkeypatch):
    monkeypatch.setattr(plan, "_credentials", lambda: json.dumps({"other": {}}))
    assert plan.auth_kind() == plan.API_KEY


def test_no_credentials_with_a_key_in_the_environment_reads_as_an_api_key(monkeypatch):
    monkeypatch.setattr(plan, "_credentials", lambda: None)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    assert plan.auth_kind() == plan.API_KEY


def test_credentials_that_cannot_be_read_are_unknown_not_an_api_key(monkeypatch):
    """A locked Keychain must not flip the mode line from limits to a spend figure."""
    monkeypatch.setattr(plan, "_credentials", lambda: None)
    for name in plan.API_KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    assert plan.auth_kind() == plan.UNKNOWN


def test_malformed_credentials_are_unknown(monkeypatch):
    monkeypatch.setattr(plan, "_credentials", lambda: "{not json")
    assert plan.auth_kind() == plan.UNKNOWN


# --------------------------------------------------------------------------- #
# The loader the workspace calls
# --------------------------------------------------------------------------- #
def test_load_returns_none_without_a_bus_db(root, monkeypatch, tmp_path):
    monkeypatch.setattr(spend.store, "db_path", lambda: tmp_path / "nothing.db")
    assert spend.load(1_800_000_000.0) is None


def test_load_ingests_then_prices(root, conn, monkeypatch):
    monkeypatch.setattr(spend.billing, "admin_key", lambda cwd=None: None)
    now = 1_800_000_000.0
    (root / "proj" / "a.jsonl").write_text(line("m1", at(now, 60), output=1_000_000))
    found = spend.load(now)
    assert found is not None
    assert found.spend.all_time.cost == pytest.approx(prices.PRICES["claude-opus-5-5"].output)
    assert found.any_billed is False  # no admin key: no billed column


def test_the_billed_column_appears_when_the_cost_api_answers(root, conn, monkeypatch):
    """The billed figure sits beside the computed one, never replacing it: they answer
    different questions (the organization's bill against this machine's tokens).
    """
    now = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC).timestamp()
    (root / "proj" / "a.jsonl").write_text(line("m1", at(now, 60), output=1_000_000))
    monkeypatch.setattr(spend.billing, "admin_key", lambda cwd=None: "sk-ant-admin-test")
    monkeypatch.setattr(
        spend.billing,
        "_request",
        lambda key, params: {
            "data": [
                {
                    "starting_at": "2026-11-12T00:00:00Z",
                    "cost": {"amount": "25000.000000"},  # $250
                }
            ],
            "has_more": False,
            "data_refreshed_at": "2026-11-13T06:00:00Z",
        },
    )
    found = spend.load(now)
    assert found is not None
    assert found.any_billed is True
    assert found.billed_week == pytest.approx(250.0 * 11)  # one row per 31-day chunk
    assert found.billed_today is None  # the horizon has not reached today
    assert found.spend.all_time.cost > 0  # the computed figure is still there


# --------------------------------------------------------------------------- #
# Split by model
# --------------------------------------------------------------------------- #
def test_by_model_splits_a_period_and_puts_the_dearest_first(root, conn):
    now = 1_800_000_000.0
    (root / "proj" / "a.jsonl").write_text(
        line("cheap", at(now, 60), model="claude-haiku-5-5", output=1_000_000)
        + line("dear", at(now, 60), model="claude-fable-5-2", output=1_000_000)
    )
    usage.ingest(conn)
    found = usage.by_model(conn, 0.0)
    assert [m.model for m in found] == ["claude-fable-5-2", "claude-haiku-5-5"]
    assert found[0].cost == pytest.approx(prices.PRICES["claude-fable-5-2"].output)
    assert found[0].tokens == found[1].tokens == 1_000_000  # same tokens, 80x the cost
    assert found[0].cost > 50 * found[1].cost


def test_a_model_without_rates_keeps_its_tokens_and_reports_no_cost(root, conn):
    """Its tokens are real and its cost is not known, which is not the same as free."""
    now = 1_800_000_000.0
    (root / "proj" / "a.jsonl").write_text(
        line("m1", at(now, 60), model="claude-opus-9", output=500_000)
    )
    usage.ingest(conn)
    found = usage.by_model(conn, 0.0)
    assert (found[0].tokens, found[0].cost, found[0].priced) == (500_000, 0.0, False)


def test_by_model_can_be_narrowed_to_one_conversation(root, conn):
    now = 1_800_000_000.0
    (root / "proj" / "a.jsonl").write_text(line("m1", at(now, 60), output=1000))
    usage.ingest(conn)
    conn.execute("UPDATE usage_messages SET session_id = 'other' WHERE message_id = 'm1'")
    conn.commit()
    assert usage.by_model(conn, 0.0, ("mine",)) == []
    assert [m.model for m in usage.by_model(conn, 0.0, ("other",))] == ["claude-opus-5-5"]
