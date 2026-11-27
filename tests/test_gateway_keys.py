"""Several named Bedrock keys: each is read from ``keys.toml``, and its tokens are counted."""

# ruff: noqa: F811 (a fixture imported from test_gateway, then named as a parameter)

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cabildo import cli, config, gateway, gateway_usage, store, usage
from cabildo.workspace import upstreams
from tests.test_eventstream import chunk
from tests.test_gateway import fakes, post, run  # noqa: F401 (fixtures)


@pytest.fixture
def keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "cabildo" / "keys.toml"
    path.parent.mkdir()
    path.write_text('[bedrock.keys]\nalpha = "key-alpha"\nbeta = "key-beta"\n')
    return path


def two_keys(fakes: dict[str, Any], **beta: Any) -> gateway.Settings:
    def entry(name: str, fake: str) -> dict[str, Any]:
        return {"kind": "key", "base_url": fakes[fake].url, "key": f"bedrock:{name}"}

    return gateway.parse(
        {"upstream": {"alpha": entry("alpha", "sub"), "beta": {**entry("beta", "bed"), **beta}}}
    )


def message(**counts: int) -> bytes:
    return json.dumps({"type": "message", "content": [], "usage": counts}).encode()


def test_a_named_key_is_read_from_the_file_each_time(keys_file: Path) -> None:
    keys = gateway.Keys()
    ref = gateway.parse_key_ref("bedrock:alpha")
    assert keys.get(ref) == "key-alpha"
    keys_file.write_text('bedrock.keys.alpha = "changed"\n')
    assert keys.get(ref) == "changed"


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ('[bedrock.keys]\nbeta = "key-beta"\n', "no key 'alpha'"),
        ('bedrock = "key-beta"\n', "no key 'alpha'"),
        ('[bedrock.keys]\nalpha = "key-beta\n', "not valid TOML"),
    ],
)
def test_a_missing_name_or_a_bad_file_is_refused_without_showing_a_key(
    keys_file: Path, text: str, said: str
) -> None:
    keys_file.write_text(text)
    with pytest.raises(gateway.GatewayError, match=said) as err:
        gateway.Keys().get(gateway.parse_key_ref("bedrock:alpha"))
    assert "key-beta" not in str(err.value)
    assert err.value.__cause__ is None


def test_each_key_fails_over_to_the_next_and_counts_its_own_tokens(
    keys_file: Path,
    fakes,
    run,
) -> None:
    fakes["sub"].queue = [(200, message(input_tokens=10, output_tokens=2))]
    fakes["sub"].status = 429
    fakes["bed"].body = message(input_tokens=100, output_tokens=7, cache_read_input_tokens=40)
    got: list[gateway_usage.Reply] = []
    port = run(gateway.Gateway(two_keys(fakes), record=got.append))

    assert post(port)[0] == post(port)[0] == 200

    assert fakes["sub"].header(0, "x-api-key") == ["key-alpha"]
    assert fakes["bed"].header(0, "x-api-key") == ["key-beta"]
    assert [(r.upstream, r.input, r.output, r.cache_read) for r in got] == [
        ("alpha", 10, 2, 0),
        ("beta", 100, 7, 40),
    ]
    assert {r.model for r in got} == {"claude-opus-5-5"}


def test_each_key_serves_only_the_models_it_lists(keys_file: Path, fakes, run) -> None:
    fakes["sub"].status = 429
    port = run(gateway.Gateway(two_keys(fakes, models={"claude-haiku-4-5": "anthropic.haiku"})))
    assert post(port)[0] == 429, "beta does not list the model, so alpha's answer comes back"
    assert fakes["bed"].seen == []


def test_counting_tokens_and_refusals_are_not_recorded(
    keys_file: Path,
    fakes,
    run,
) -> None:
    fakes["sub"].body = b'{"input_tokens": 12}'
    got: list[gateway_usage.Reply] = []
    port = run(gateway.Gateway(two_keys(fakes), record=got.append))
    post(port, path="/v1/messages/count_tokens")
    fakes["sub"].status, fakes["sub"].body = 400, message(input_tokens=1)
    post(port)
    assert got == []


def test_a_streamed_reply_is_counted_however_it_is_cut() -> None:
    events = [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 30,
                    "cache_creation": {"ephemeral_1h_input_tokens": 20},
                }
            },
        },
        {"type": "content_block_delta", "delta": {"text": 'the word "usage"'}},
        {"type": "message_delta", "usage": {"output_tokens": 90, "input_tokens": None}},
    ]
    raw = b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)
    counter = gateway_usage.Counter(streamed=True)
    for i in range(0, len(raw), 7):
        counter.feed(raw[i : i + 7])

    found = counter.reply("alpha", "claude-opus-5-5", 1.0)

    assert found == gateway_usage.Reply(
        1.0, "alpha", "claude-opus-5-5", 5, 90, 0, 30, 20, fast=False
    )


def test_a_streamed_invoke_reply_is_counted(keys_file: Path, fakes, run) -> None:
    fakes["bed"].body = chunk(
        {"type": "message_start", "message": {"usage": {"input_tokens": 4}}}
    ) + chunk({"type": "message_delta", "usage": {"output_tokens": 9}})
    fakes["bed"].headers = {"Content-Type": "application/vnd.amazon.eventstream"}
    settings = gateway.parse(
        {
            "upstream": {
                "eu": {
                    "kind": "bedrock-invoke",
                    "base_url": fakes["bed"].url,
                    "key": "bedrock:beta",
                    "models": {"claude-opus-5-5": "eu.anthropic.claude-opus-5"},
                }
            }
        }
    )
    got: list[gateway_usage.Reply] = []
    port = run(gateway.Gateway(settings, record=got.append))

    post(port, {"model": "claude-opus-5-5", "stream": True, "messages": []})

    assert [(r.upstream, r.input, r.output) for r in got] == [("eu", 4, 9)]


def test_spent_prices_each_upstream_apart(home: Path) -> None:
    now = time.time()
    log: list[str] = []
    record = gateway_usage.recorder(log.append)
    million = 1_000_000
    record(gateway_usage.Reply(now, "alpha", "claude-opus-5-5", million, 0, 0, 0, 0, fast=False))
    record(
        gateway_usage.Reply(
            now - 3 * usage.DAY, "alpha", "claude-opus-5-5", 0, million, 0, 0, 0, fast=False
        )
    )
    record(gateway_usage.Reply(now, "max", "claude-opus-5-5", 1_500, 0, 0, 0, 0, fast=False))

    found = gateway_usage.spent(("alpha", "beta", "max"), now)

    assert log == []
    assert [s.upstream for s in found] == ["alpha", "max"]
    assert upstreams.spent_text(found[0], priced=True) == "today $4.00 · 7d $24.00"
    assert upstreams.spent_text(found[1], priced=False) == "today 2k tokens · 7d 2k tokens"
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM gateway_usage").fetchone()[0] == 3


def test_gateway_status_shows_the_cost_under_each_name(
    keys_file: Path,
    fakes,
    run,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conf = keys_file.parent / "config.toml"
    conf.write_text(
        f'[gateway.upstream.alpha]\nkind = "key"\nbase_url = "{fakes["sub"].url}"\n'
        'key = "bedrock:alpha"\n'
    )
    monkeypatch.setattr(config, "_global_path", lambda: conf)
    fakes["sub"].body = message(input_tokens=500_000)
    settings = gateway.parse(config.gateway_table())
    port = run(gateway.Gateway(settings, record=gateway_usage.recorder(print)))
    post(port)

    assert cli.main(["gateway", "status", "--port", str(port)]) == 0

    out = capsys.readouterr().out
    assert "alpha" in out and "today $2.00 · 7d $2.00" in out
