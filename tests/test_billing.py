"""``billing``: the organization's billed cost, cached, and what it refuses to invent."""

from __future__ import annotations

import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from cabildo import billing

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def row(day: str, cents: str) -> dict:
    return {
        "starting_at": f"{day}T00:00:00Z",
        "ending_at": f"{day}T23:59:59Z",
        "group": {"model": None},
        "cost": {"amount": cents, "list_amount": cents, "currency": "USD", "requests": 1},
    }


def answer(rows: list[dict], refreshed: str | None = "2026-11-14T06:00:00Z", **extra) -> dict:
    return {"data": rows, "has_more": False, "next_page": None, "data_refreshed_at": refreshed} | (
        extra
    )


@pytest.fixture
def api(monkeypatch):
    """Capture every request and reply with queued answers."""
    sent: list[list[tuple[str, str]]] = []
    queue: list[dict] = []

    def fake(key, params):
        sent.append(params)
        return queue.pop(0) if queue else answer([])

    monkeypatch.setattr(billing, "_request", fake)
    monkeypatch.setattr(billing, "admin_key", lambda cwd=None: "sk-ant-admin-test")
    return sent, queue


# --------------------------------------------------------------------------- #
# Reading the API's answer
# --------------------------------------------------------------------------- #
def test_amounts_are_cents_and_become_dollars(api):
    sent, queue = api
    queue.append(answer([row("2026-11-13", "41280.000000")]))
    found = billing.fetch("k", NOW - timedelta(days=1), NOW)
    assert found.days == (billing.Day("2026-11-13", 412.80),)


def test_a_malformed_row_is_skipped_not_counted_as_zero(api):
    sent, queue = api
    queue.append(
        answer(
            [
                row("2026-11-12", "100.000000"),
                {"starting_at": "2026-11-13T00:00:00Z", "cost": {"amount": "oops"}},
                {"cost": {"amount": "500.000000"}},  # no day
            ]
        )
    )
    found = billing.fetch("k", NOW - timedelta(days=3), NOW)
    assert found.days == (billing.Day("2026-11-12", 1.0),)


def test_a_range_is_split_into_spans_the_api_accepts(api):
    sent, _ = api
    billing.fetch("k", NOW - timedelta(days=70), NOW)
    assert len(sent) == 3  # 31 + 31 + 8 days
    for params in sent:
        p = dict(params)
        start = datetime.fromisoformat(p["starting_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(p["ending_at"].replace("Z", "+00:00"))
        assert end - start <= billing.MAX_SPAN
        assert p["time_bucket_width"] == "1d"


def test_the_range_never_starts_before_the_api_has_data(api):
    sent, _ = api
    billing.fetch("k", datetime(2020, 1, 1, tzinfo=UTC), NOW)
    first = datetime.fromisoformat(dict(sent[0])["starting_at"].replace("Z", "+00:00"))
    assert first >= billing.EARLIEST
    assert first >= NOW - billing.MAX_LOOKBACK - timedelta(days=1)


def test_pages_are_followed_until_has_more_is_false(api):
    sent, queue = api
    queue.append(answer([row("2026-11-12", "100.000000")], has_more=True, next_page="page_2") | {})
    queue.append(answer([row("2026-11-13", "200.000000")]))
    found = billing.fetch("k", NOW - timedelta(days=3), NOW)
    assert {d.day for d in found.days} == {"2026-11-12", "2026-11-13"}
    assert dict(sent[1])["page"] == "page_2"


def test_an_empty_answer_is_not_zero_spend(api):
    sent, queue = api
    queue.append(answer([], refreshed=None))
    found = billing.fetch("k", NOW - timedelta(days=1), NOW)
    assert found.days == () and found.refreshed_at is None


# --------------------------------------------------------------------------- #
# Failures a human can act on
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("code", "says"),
    [(401, "Admin API key"), (403, "Admin API key"), (404, "not available"), (429, "rate limit")],
)
def test_http_failures_explain_themselves(monkeypatch, code, says):
    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, code, "no", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(billing.urllib.request, "urlopen", boom)
    with pytest.raises(billing.BillingError) as got:
        billing.fetch("k", NOW - timedelta(days=1), NOW)
    assert says in str(got.value)


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #
def test_a_finished_day_is_stored_final_and_not_asked_for_again(api, conn):
    sent, queue = api
    # the horizon is past the 12th's end, so that day is complete; the 13th is not
    queue.append(
        answer(
            [row("2026-11-12", "100.000000"), row("2026-11-13", "200.000000")],
            refreshed="2026-11-13T06:00:00Z",
        )
    )
    billing.refresh(conn, None, NOW)
    final = dict(conn.execute("SELECT day, final FROM billed_days").fetchall())
    assert (final["2026-11-12"], final["2026-11-13"]) == (1, 0)

    queue.append(answer([row("2026-11-13", "250.000000")], refreshed="2026-11-14T06:00:00Z"))
    billing.refresh(conn, None, NOW + timedelta(seconds=billing.REFRESH + 1))
    assert dict(sent[-1])["starting_at"].startswith("2026-11-13")  # not the 12th again
    assert billing.stored(conn).total() == pytest.approx(1.0 + 2.5)  # the 13th was revised


def test_a_fresh_cache_is_not_refetched(api, conn):
    """A cold cache reads the whole history the API offers, in the spans it allows; the
    next tick a few seconds later must ask for nothing at all.
    """
    sent, queue = api
    queue.append(answer([row("2026-11-13", "100.000000")]))
    billing.refresh(conn, None, NOW)
    cold = len(sent)
    assert cold > 1  # 2026-01-01 to now, cut into 31-day spans
    billing.refresh(conn, None, NOW + timedelta(seconds=10))
    assert len(sent) == cold


def test_a_failed_fetch_backs_off_and_keeps_the_cache(api, conn, monkeypatch):
    sent, queue = api
    queue.append(answer([row("2026-11-13", "100.000000")]))
    billing.refresh(conn, None, NOW)

    def boom(key, params):
        raise billing.BillingError("the cost API answered 500")

    monkeypatch.setattr(billing, "_request", boom)
    later = NOW + timedelta(seconds=billing.REFRESH + 1)
    kept = billing.refresh(conn, None, later)
    assert kept.total() == pytest.approx(1.0)  # the cache survives the failure
    assert billing.last_error(conn) == "the cost API answered 500"
    # and the next tick does not hammer the API
    monkeypatch.setattr(billing, "_request", lambda key, params: answer([]))
    before = len(sent)
    billing.refresh(conn, None, later + timedelta(seconds=1))
    assert len(sent) == before


def test_without_a_key_nothing_is_fetched(conn, monkeypatch):
    monkeypatch.setattr(billing, "admin_key", lambda cwd=None: None)
    called = []
    monkeypatch.setattr(billing, "fetch", lambda *a, **k: called.append(1))
    assert billing.refresh(conn, None, NOW).days == ()
    assert not called


# --------------------------------------------------------------------------- #
# The credential
# --------------------------------------------------------------------------- #
def test_the_key_comes_from_the_named_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    monkeypatch.setenv(billing.DEFAULT_KEY_ENV, "  sk-ant-admin-abc  ")
    assert billing.admin_key(None) == "sk-ant-admin-abc"


def test_no_key_configured_is_none_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    monkeypatch.delenv(billing.DEFAULT_KEY_ENV, raising=False)
    assert billing.admin_key(None) is None


def test_a_key_command_is_run_without_a_shell(monkeypatch, tmp_path):
    """The command comes from a config file, so it is split and executed directly: a
    shell would make every character in that file executable.
    """
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    monkeypatch.delenv(billing.DEFAULT_KEY_ENV, raising=False)
    (tmp_path / "config.toml").write_text('[billing]\nkey_command = "echo sk-ant-admin-xyz"\n')
    assert billing.admin_key(None) == "sk-ant-admin-xyz"


def test_a_failing_key_command_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    monkeypatch.delenv(billing.DEFAULT_KEY_ENV, raising=False)
    (tmp_path / "config.toml").write_text('[billing]\nkey_command = "false"\n')
    assert billing.admin_key(None) is None


def test_the_key_is_never_written_to_the_bus_db(api, conn):
    _, queue = api
    queue.append(answer([row("2026-11-13", "100.000000")]))
    billing.refresh(conn, None, NOW)
    dumped = "\n".join(line for line in conn.iterdump())
    assert "sk-ant-admin" not in dumped


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
def test_since_counts_from_a_days_start(api):
    _, queue = api
    queue.append(
        answer([row("2026-11-12", "100.000000"), row("2026-11-14", "300.000000")]),
    )
    found = billing.fetch("k", NOW - timedelta(days=5), NOW)
    assert found.since(datetime(2026, 11, 13, tzinfo=UTC)) == pytest.approx(3.0)
    assert found.total() == pytest.approx(4.0)
