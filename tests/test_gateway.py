"""The gateway fails over between upstreams before the client sees a 429."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cabildo import gateway, onepassword
from tests.test_eventstream import chunk


class Fake:
    """An upstream that records what it got and answers from a script."""

    def __init__(
        self, status: int = 200, body: bytes = b'{"ok": true}', headers: dict | None = None
    ) -> None:
        self.status, self.body, self.headers = status, body, headers or {}
        self.seen: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                outer.seen.append(
                    {
                        "path": self.path,
                        "headers": [(k.lower(), v) for k, v in self.headers.items()],
                        "body": self.rfile.read(n),
                    }
                )
                self.send_response(outer.status)
                for k, v in outer.headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in outer.headers:
                    self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def header(self, i: int, name: str) -> list[str]:
        return [v for k, v in self.seen[i]["headers"] if k == name]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fakes() -> Iterator[dict[str, Fake]]:
    made = {"sub": Fake(), "bed": Fake()}
    yield made
    for f in made.values():
        f.close()


@pytest.fixture(autouse=True)
def bed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BED_KEY", "bedrock-secret")


def settings(fakes: dict[str, Fake], **extra: Any) -> gateway.Settings:
    bed = {
        "kind": "key",
        "base_url": fakes["bed"].url + "/anthropic",
        "key": "env:BED_KEY",
        **extra.pop("bed", {}),
    }
    return gateway.parse(
        {
            "upstream": {
                "sub": {"kind": "passthrough", "base_url": fakes["sub"].url},
                "bed": bed,
            },
            **extra,
        }
    )


def serve(gw: gateway.Gateway) -> tuple[ThreadingHTTPServer, int]:
    server = gateway.make_server(gw, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_port


def post(
    port: int,
    body: dict | bytes | None = None,
    headers: list[tuple[str, str]] | None = None,
    path: str = "/v1/messages",
) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    raw = (
        body
        if isinstance(body, bytes)
        else json.dumps(body or {"model": "claude-opus-5-5"}).encode()
    )
    conn.putrequest("POST", path)
    for k, v in (
        headers
        if headers is not None
        else [
            ("Authorization", "Bearer oauth-token"),
            ("anthropic-beta", "oauth-2025"),
        ]
    ):
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(len(raw)))
    conn.endheaders(raw)
    r = conn.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    conn.close()
    return out


@pytest.fixture
def run(fakes) -> Iterator[Any]:
    started: list[ThreadingHTTPServer] = []

    def go(gw: gateway.Gateway) -> int:
        server, port = serve(gw)
        started.append(server)
        return port

    yield go
    for s in started:
        s.shutdown()


# --- routing ---------------------------------------------------------------------------------


def test_the_first_upstream_gets_the_request_with_the_clients_own_login(fakes, run) -> None:
    port = run(gateway.Gateway(settings(fakes)))
    status, _h, body = post(port)
    assert (status, json.loads(body)) == (200, {"ok": True})
    assert fakes["sub"].header(0, "authorization") == ["Bearer oauth-token"]
    assert fakes["sub"].header(0, "anthropic-beta") == ["oauth-2025"]
    assert fakes["bed"].seen == []


def test_a_429_moves_the_request_to_the_next_upstream_with_its_own_key(fakes, run) -> None:
    fakes["sub"].status, fakes["sub"].headers = 429, {"retry-after": "120"}
    gw = gateway.Gateway(settings(fakes))
    port = run(gw)
    assert post(port)[0] == post(port)[0] == 200
    bed = fakes["bed"].seen[0]
    assert bed["path"] == "/anthropic/v1/messages"
    assert fakes["bed"].header(0, "x-api-key") == ["bedrock-secret"]
    assert fakes["bed"].header(0, "authorization") == []  # the client's token never leaves
    assert len(fakes["sub"].seen) == 1, "the second request skips the upstream that was set aside"
    rows = {r.name: r for r in gw.router.status()}
    assert not rows["sub"].ready and rows["sub"].set_aside_s > 100
    assert rows["sub"].failovers == 1 and rows["bed"].requests == 2


def test_when_every_upstream_is_exhausted_the_client_gets_the_real_answer(fakes, run) -> None:
    for f in fakes.values():
        f.status, f.body = 429, b'{"error": "limit"}'
    port = run(gateway.Gateway(settings(fakes)))
    status, _h, body = post(port)
    assert status == 429 and b"limit" in body


def test_a_400_that_is_not_about_credit_goes_to_the_client(fakes, run) -> None:
    fakes["sub"].status, fakes["sub"].body = 400, b'{"error": "bad request"}'
    gw = gateway.Gateway(settings(fakes))
    port = run(gw)
    status, _h, body = post(port)
    assert status == 400 and b"bad request" in body
    assert fakes["bed"].seen == []
    assert all(r.ready for r in gw.router.status())


def test_a_low_credit_400_fails_over(fakes, run) -> None:
    fakes["sub"].status = 400
    fakes["sub"].body = b'{"error": {"message": "Your credit balance is too low"}}'
    port = run(gateway.Gateway(settings(fakes)))
    assert post(port)[0] == 200 and len(fakes["bed"].seen) == 1


def test_a_401_on_the_subscription_goes_to_the_client_and_sets_nothing_aside(fakes, run) -> None:
    fakes["sub"].status, fakes["sub"].body = 401, b'{"error": "expired"}'
    gw = gateway.Gateway(settings(fakes))
    port = run(gw)
    assert post(port)[0] == 401
    assert fakes["bed"].seen == []
    assert all(r.ready for r in gw.router.status()), "one session's login is not the account's"


def test_a_401_on_a_key_upstream_sets_it_aside_for_everyone(fakes, run) -> None:
    fakes["sub"].status = 429
    fakes["bed"].status = 401
    gw = gateway.Gateway(settings(fakes))
    port = run(gw)
    assert post(port)[0] == 401
    assert not {r.name: r for r in gw.router.status()}["bed"].ready


def test_a_429_is_set_aside_for_that_model_only(fakes, run) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(settings(fakes))
    port = run(gw)
    post(port, {"model": "opus"})
    post(port, {"model": "haiku"})
    assert len(fakes["sub"].seen) == 2, "haiku still tried the subscription"
    post(port, {"model": "opus"})
    assert len(fakes["sub"].seen) == 2, "opus did not"


def test_the_unified_reset_header_sets_the_cooldown(fakes, run) -> None:
    reset = str(time.time() + 3 * 3600)
    fakes["sub"].status, fakes["sub"].headers = 429, {"anthropic-ratelimit-unified-reset": reset}
    gw = gateway.Gateway(settings(fakes))
    port = run(gw)
    post(port)
    left = {r.name: r for r in gw.router.status()}["sub"].set_aside_s
    assert 3 * 3600 - 60 < left <= 3 * 3600


def test_a_529_fails_over_and_waits_a_short_time(fakes, run) -> None:
    fakes["sub"].status = 529
    gw = gateway.Gateway(settings(fakes))
    port = run(gw)
    assert post(port)[0] == 200
    assert {r.name: r for r in gw.router.status()}["sub"].set_aside_s <= 30


def test_classify_cooldowns() -> None:
    c = gateway.classify
    assert c(429, {"Retry-After": "90"}, b"").cooldown_s == 90  # type: ignore[union-attr]
    assert c(429, {}, b"", kind="key").cooldown_s == gateway.RATE_COOLDOWN_KEY_S  # type: ignore[union-attr]
    assert c(429, {}, b"", kind="passthrough").cooldown_s == gateway.RATE_COOLDOWN_PASSTHROUGH_S  # type: ignore[union-attr]
    assert c(429, {}, b"").scope == "model"  # type: ignore[union-attr]
    assert c(402, {}, b"").scope == "upstream"  # type: ignore[union-attr]
    assert c(500, {}, b"") is None
    assert c(401, {}, b"", kind="passthrough") is None
    assert c(403, {}, b"", kind="passthrough") is None
    assert c(403, {}, b"", kind="key").scope == "model"  # type: ignore[union-attr]


def test_an_unreadable_key_sets_that_upstream_aside_and_the_real_answer_comes_back(
    fakes, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BED_KEY")
    fakes["sub"].status = 429
    port = run(gateway.Gateway(settings(fakes)))
    assert post(port)[0] == 429  # nothing could serve it: the real 429, no secret in sight


def test_an_unreachable_upstream_is_skipped(fakes, run) -> None:
    base = settings(fakes)
    dead = gateway.Upstream("gone", "passthrough", "http://127.0.0.1:9")
    port = run(gateway.Gateway(gateway.Settings(base.port, (dead, *base.upstreams))))
    assert post(port)[0] == 200


def test_a_timeout_after_the_request_was_sent_is_not_retried(fakes, run) -> None:
    class Slow(http.client.HTTPConnection):
        def getresponse(self, *a: Any, **k: Any) -> Any:
            raise TimeoutError

    def connect(up: gateway.Upstream, timeout: float) -> http.client.HTTPConnection:
        parts = up.base_url.split("//")[1].split("/")[0].split(":")
        cls = Slow if up.name == "sub" else http.client.HTTPConnection
        return cls(parts[0], int(parts[1]), timeout=timeout)

    port = run(gateway.Gateway(settings(fakes), connect=connect))
    assert post(port)[0] == 504
    assert fakes["bed"].seen == [], "a second try could bill twice"


# --- request and response handling --------------------------------------------------------------


def test_a_chunked_request_body_is_decoded(fakes, run) -> None:
    port = run(gateway.Gateway(settings(fakes)))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/v1/messages")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    for part in (b'{"model": "x",', b' "n": 1}'):
        conn.send(b"%x\r\n%s\r\n" % (len(part), part))
    conn.send(b"0\r\n\r\n")
    assert conn.getresponse().status == 200
    assert json.loads(fakes["sub"].seen[0]["body"]) == {"model": "x", "n": 1}


def test_the_response_keeps_its_content_length(fakes, run) -> None:
    port = run(gateway.Gateway(settings(fakes)))
    _s, headers, body = post(port)
    assert headers["Content-Length"] == str(len(body))


def test_repeated_beta_headers_are_merged_and_oauth_is_dropped_for_a_key(fakes, run) -> None:
    fakes["sub"].status = 429
    port = run(gateway.Gateway(settings(fakes)))
    post(
        port,
        headers=[
            ("Authorization", "Bearer t"),
            ("anthropic-beta", "oauth-2025-04-20, prompt-caching-x"),
            ("anthropic-beta", "interleaved-thinking-y"),
        ],
    )
    assert fakes["sub"].header(0, "anthropic-beta") == [
        "oauth-2025-04-20,prompt-caching-x,interleaved-thinking-y"
    ]
    assert fakes["bed"].header(0, "anthropic-beta") == ["prompt-caching-x,interleaved-thinking-y"]


def test_a_streamed_body_arrives_whole(fakes, run) -> None:
    events = b"".join(b"event: x\ndata: %d\n\n" % i for i in range(200))
    fakes["sub"].body, fakes["sub"].headers = events, {"Content-Type": "text/event-stream"}
    port = run(gateway.Gateway(settings(fakes)))
    status, headers, body = post(port)
    assert status == 200 and body == events
    assert headers["Content-Type"] == "text/event-stream"


# --- what a request may reach -------------------------------------------------------------------


def test_models_are_renamed_for_the_upstream_that_maps_them(fakes, run) -> None:
    fakes["sub"].status = 429
    port = run(
        gateway.Gateway(
            settings(fakes, bed={"models": {"claude-opus-5-5": "anthropic.claude-opus-5-5"}})
        )
    )
    post(port, {"model": "claude-opus-5-5", "max_tokens": 5})
    assert json.loads(fakes["bed"].seen[0]["body"]) == {
        "model": "anthropic.claude-opus-5-5",
        "max_tokens": 5,
    }


def test_a_models_table_is_an_allowlist_and_skipping_sets_nothing_aside(fakes, run) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(settings(fakes, bed={"models": {"opus": "anthropic.opus"}}))
    port = run(gw)
    status, _h, body = post(port, {"model": "haiku"})
    assert status == 429, "bedrock cannot serve haiku: the real 429 comes back"
    assert fakes["bed"].seen == []
    assert {r.name: r for r in gw.router.status()}["bed"].ready


def test_a_request_with_a_server_tool_skips_an_upstream_without_them(fakes, run) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(settings(fakes, bed={"server_tools": False}))
    port = run(gw)
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    assert post(port, {"model": "m", "tools": tools})[0] == 429
    assert fakes["bed"].seen == []
    plain = [{"name": "bash", "input_schema": {}}, {"type": "custom", "name": "x"}]
    assert post(port, {"model": "m", "tools": plain})[0] == 200
    assert len(fakes["bed"].seen) == 1


def test_a_key_upstream_only_gets_messages_paths(fakes, run) -> None:
    fakes["sub"].status = 429
    port = run(gateway.Gateway(settings(fakes)))
    post(port, b"{}", path="/v1/files")
    assert fakes["bed"].seen == []
    post(port, b'{"model": "m"}', path="/v1/messages/count_tokens")
    assert fakes["bed"].seen[0]["path"] == "/anthropic/v1/messages/count_tokens"


def test_when_every_upstream_is_skipped_the_answer_says_so(fakes, run) -> None:
    only_bed = gateway.parse(
        {
            "upstream": {
                "bed": {
                    "kind": "key",
                    "base_url": fakes["bed"].url,
                    "key": "env:BED_KEY",
                    "server_tools": False,
                }
            }
        }
    )
    port = run(gateway.Gateway(only_bed))
    tools = [{"type": "web_search_20250305", "name": "w"}]
    status, _h, body = post(port, {"model": "m", "tools": tools})
    assert status == 502 and b"no upstream can serve" in body


# --- who may call it ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        [("Origin", "https://evil.example")],
        [("Host", "evil.example")],
        [("Host", "127.0.0.1:1")],
    ],
)
def test_a_browser_or_a_foreign_host_is_refused(fakes, run, extra) -> None:
    port = run(gateway.Gateway(settings(fakes)))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/v1/messages", skip_host=any(k == "Host" for k, _ in extra))
    for k, v in extra:
        conn.putheader(k, v)
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    assert conn.getresponse().status == 403
    assert fakes["sub"].seen == []


def test_a_token_is_required_when_configured_and_never_forwarded(
    fakes, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GW_TOKEN", "s3cret")
    port = run(gateway.Gateway(settings(fakes, token="env:GW_TOKEN")))  # noqa: S106
    assert post(port)[0] == 401
    ok = post(port, headers=[("Authorization", "Bearer t"), (gateway.TOKEN_HEADER, "s3cret")])
    assert ok[0] == 200
    assert fakes["sub"].header(0, gateway.TOKEN_HEADER) == []


# --- status, use, reload ---------------------------------------------------------------------


def test_the_status_endpoint_lists_the_upstreams_and_the_forced_one(fakes, run, tmp_path) -> None:
    use = tmp_path / "use.json"
    gateway.write_force(use, gateway.Force("bed", time.time() + 600))
    port = run(gateway.Gateway(settings(fakes), use_file=use))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/_cabildo/status")
    data = json.loads(conn.getresponse().read())
    assert data["forced"] == "bed"
    assert [u["name"] for u in data["upstreams"]] == ["sub", "bed"]
    assert [u["forced"] for u in data["upstreams"]] == [False, True]


def test_a_forced_upstream_goes_first_and_the_others_stay_as_fallbacks(
    fakes, run, tmp_path
) -> None:
    use = tmp_path / "use.json"
    gateway.write_force(use, gateway.Force("bed", time.time() + 600))
    port = run(gateway.Gateway(settings(fakes), use_file=use))
    post(port)
    assert len(fakes["bed"].seen) == 1 and fakes["sub"].seen == []
    fakes["bed"].status = 429
    post(port)
    assert len(fakes["sub"].seen) == 1, "the forced one ran out: the next one serves"


def test_a_force_expires_and_auto_clears_it(tmp_path) -> None:
    use = tmp_path / "use.json"
    gateway.write_force(use, gateway.Force("bed", 100.0))
    assert gateway.read_force(use, 50.0) == gateway.Force("bed", 100.0)
    assert gateway.read_force(use, 101.0) is None
    gateway.write_force(use, None)
    assert gateway.read_force(use, 0.0) is None
    assert gateway.read_force(tmp_path / "missing.json", 0.0) is None


def test_parse_duration() -> None:
    assert gateway.parse_duration("90s") == 90
    assert gateway.parse_duration("2h") == 7200
    for bad in ("", "2", "h", "2 weeks"):
        with pytest.raises(gateway.GatewayError):
            gateway.parse_duration(bad)


def test_reconfigure_keeps_an_upstream_set_aside_and_its_counts(fakes) -> None:
    gw = gateway.Gateway(settings(fakes))
    first = gw.settings.upstreams[0]
    gw.router.set_aside(first, gateway.Exhausted("limit", 100))
    gw.router.served(first)
    only_sub = gateway.parse(
        {"upstream": {"sub": {"kind": "passthrough", "base_url": fakes["sub"].url}}}
    )
    gw.reconfigure(only_sub)
    rows = gw.router.status()
    assert [r.name for r in rows] == ["sub"]
    assert not rows[0].ready and rows[0].requests == 1


# --- config and keys ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        {},
        {"upstream": {"a": {"kind": "nope", "base_url": "http://x"}}},
        {"upstream": {"a": {"kind": "key", "base_url": "http://x"}}},
        {"upstream": {"a": {"kind": "key", "base_url": "http://x", "key": "plain"}}},
        {"upstream": {"a": {"kind": "key", "base_url": "http://x", "key": "env:"}}},
        {"upstream": {"a": {"kind": "passthrough", "base_url": "ftp://x"}}},
        {"upstream": {"a b": {"kind": "passthrough", "base_url": "http://x"}}},
        {
            "policy": "round_robin",
            "upstream": {"a": {"kind": "passthrough", "base_url": "http://x"}},
        },
        {"port": 99999, "upstream": {"a": {"kind": "passthrough", "base_url": "http://x"}}},
        {"token": "plain", "upstream": {"a": {"kind": "passthrough", "base_url": "http://x"}}},
        {"upstream": {"a": {"kind": "key", "base_url": "http://x", "key": "env:K", "auth": "x"}}},
    ],
)
def test_a_bad_config_is_refused_with_a_reason(table: dict) -> None:
    with pytest.raises(gateway.GatewayError):
        gateway.parse(table)


def test_defaults_for_a_key_upstream() -> None:
    up = gateway.parse(
        {"upstream": {"b": {"kind": "key", "base_url": "http://x", "key": "env:K"}}}
    ).upstreams[0]
    assert up.auth == "x-api-key" and up.drop_betas == ("oauth-*",) and up.server_tools
    assert up.key == gateway.KeyRef("env", "K")


def test_keys_read_env_file_and_1password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def read_op(ref: str) -> onepassword.Secret:
        calls.append(ref)
        return onepassword.Secret("from-op")

    keys = gateway.Keys(read_op)
    monkeypatch.setenv("K", "from-env")
    (tmp_path / "k").write_text("from-file\n")
    op = gateway.parse_key_ref("op://V/I/f")
    assert keys.get(gateway.parse_key_ref("env:K")) == "from-env"
    assert keys.get(gateway.parse_key_ref(f"file:{tmp_path / 'k'}")) == "from-file"
    assert keys.get(op) == keys.get(op) == "from-op"
    assert calls == ["op://V/I/f"], "a 1Password key is read once"
    keys.forget(op)
    keys.get(op)
    assert len(calls) == 2
    for bad in ("env:NOPE_UNSET", f"file:{tmp_path / 'missing'}"):
        with pytest.raises(gateway.GatewayError):
            keys.get(gateway.parse_key_ref(bad))


def test_an_error_never_holds_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K", "")
    with pytest.raises(gateway.GatewayError) as err:
        gateway.Keys().get(gateway.parse_key_ref("env:K"))
    assert "env:" in str(err.value)


# --- the command --------------------------------------------------------------------------------


def test_gateway_use_writes_the_choice_and_auto_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cabildo import cli, config

    conf = tmp_path / "config.toml"
    conf.write_text(
        '[gateway.upstream.sub]\nkind = "passthrough"\nbase_url = "https://api.anthropic.com"\n'
        '[gateway.upstream.bed]\nkind = "key"\nbase_url = "https://x.example"\nkey = "env:K"\n'
    )
    monkeypatch.setattr(config, "_global_path", lambda: conf)
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    assert cli.main(["gateway", "use", "bed", "--for", "30m"]) == 0
    force = gateway.read_force(tmp_path / "gateway-use.json", time.time())
    assert force is not None and force.name == "bed"
    assert 1700 < force.until - time.time() <= 1800
    assert cli.main(["gateway", "use"]) == 0
    assert "using bed" in capsys.readouterr().out
    assert cli.main(["gateway", "use", "nope"]) == 2
    assert cli.main(["gateway", "use", "bed", "--for", "soon"]) == 2
    assert cli.main(["gateway", "use", "auto"]) == 0
    assert gateway.read_force(tmp_path / "gateway-use.json", time.time()) is None


# --- bedrock-invoke ------------------------------------------------------------------------------


def invoke(fakes: dict[str, Fake], **extra: Any) -> gateway.Settings:
    return gateway.parse(
        {
            "upstream": {
                "sub": {"kind": "passthrough", "base_url": fakes["sub"].url},
                "eu": {
                    "kind": "bedrock-invoke",
                    "base_url": fakes["bed"].url,
                    "key": "env:BED_KEY",
                    "models": {"claude-opus-5-5": "eu.anthropic.claude-opus-5"},
                    **extra,
                },
            }
        }
    )


def test_invoke_defaults_and_the_url_from_a_region() -> None:
    up = gateway.parse(
        {
            "upstream": {
                "eu": {
                    "kind": "bedrock-invoke",
                    "region": "eu-central-1",
                    "key": "env:K",
                    "models": {"a": "b"},
                }
            }
        }
    ).upstreams[0]
    assert up.base_url == "https://bedrock-runtime.eu-central-1.amazonaws.com"
    assert up.auth == "bearer" and not up.server_tools and up.drop_betas == ("oauth-*",)


@pytest.mark.parametrize(
    "entry",
    [
        {"key": "env:K", "models": {"a": "b"}},
        {"region": "nope", "key": "env:K", "models": {"a": "b"}},
        {"region": "eu-central-1", "models": {"a": "b"}},
        {"region": "eu-central-1", "key": "env:K"},
    ],
)
def test_invoke_needs_a_region_or_url_a_key_and_models(entry: dict) -> None:
    with pytest.raises(gateway.GatewayError):
        gateway.parse({"upstream": {"eu": {"kind": "bedrock-invoke", **entry}}})


def test_a_request_becomes_an_invoke_call(fakes, run) -> None:
    fakes["sub"].status = 429
    fakes["bed"].body = b'{"id": "msg_1", "type": "message", "content": []}'
    port = run(gateway.Gateway(invoke(fakes)))
    body = {"model": "claude-opus-5-5", "max_tokens": 5, "messages": [], "stream": False}
    status, _h, out = post(
        port,
        body,
        headers=[
            ("Authorization", "Bearer oauth"),
            ("anthropic-version", "2023-06-01"),
            ("anthropic-beta", "oauth-2025,interleaved-thinking-x"),
        ],
    )
    assert status == 200 and json.loads(out)["id"] == "msg_1"
    seen = fakes["bed"].seen[0]
    assert seen["path"] == "/model/eu.anthropic.claude-opus-5/invoke"
    assert json.loads(seen["body"]) == {
        "max_tokens": 5,
        "messages": [],
        "anthropic_version": "bedrock-2023-05-31",
        "anthropic_beta": ["interleaved-thinking-x"],
    }
    assert fakes["bed"].header(0, "authorization") == ["Bearer bedrock-secret"]
    assert fakes["bed"].header(0, "x-api-key") == []
    assert fakes["bed"].header(0, "anthropic-beta") == []
    assert fakes["bed"].header(0, "anthropic-version") == []


def test_a_streamed_answer_is_decoded_into_sse(fakes, run) -> None:
    fakes["sub"].status = 429
    events = [
        {"type": "message_start", "message": {"id": "m"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_stop"},
    ]
    fakes["bed"].body = b"".join(chunk(e) for e in events)
    fakes["bed"].headers = {"Content-Type": "application/vnd.amazon.eventstream"}
    port = run(gateway.Gateway(invoke(fakes)))
    status, headers, out = post(port, {"model": "claude-opus-5-5", "stream": True})
    assert status == 200 and headers["Content-Type"] == "text/event-stream"
    assert (
        fakes["bed"].seen[0]["path"]
        == "/model/eu.anthropic.claude-opus-5/invoke-with-response-stream"
    )
    got = [
        (blk.split(b"\n")[0], json.loads(blk.split(b"data: ")[1]))
        for blk in out.strip().split(b"\n\n")
    ]
    assert [name for name, _ in got] == [b"event: " + e["type"].encode() for e in events]
    assert [d for _, d in got] == events


def test_a_bedrock_error_is_set_aside_and_shown_in_the_anthropic_shape(fakes, run) -> None:
    for f in fakes.values():
        f.status = 429
    fakes["bed"].body = b'{"message": "Too many requests"}'
    gw = gateway.Gateway(invoke(fakes))
    port = run(gw)
    status, _h, out = post(port, {"model": "claude-opus-5-5"})
    assert status == 429
    assert json.loads(out)["error"] == {"type": "rate_limit_error", "message": "Too many requests"}
    assert not {r.name: r for r in gw.router.status()}["eu"].ready


def test_a_plain_bedrock_error_is_wrapped_too(fakes, run) -> None:
    fakes["sub"].status = 429
    fakes["bed"].status, fakes["bed"].body = 400, b'{"message": "bad field"}'
    port = run(gateway.Gateway(invoke(fakes)))
    status, headers, out = post(port, {"model": "claude-opus-5-5"})
    assert status == 400 and headers["Content-Type"] == "application/json"
    assert json.loads(out)["error"]["type"] == "invalid_request_error"


def test_invoke_skips_other_paths_and_unmapped_models(fakes, run) -> None:
    fakes["sub"].status = 429
    port = run(gateway.Gateway(invoke(fakes)))
    post(port, {"model": "claude-opus-5-5"}, path="/v1/messages/count_tokens")
    post(port, {"model": "claude-haiku-4-5"})
    post(port, {"model": "claude-opus-5-5", "tools": [{"type": "web_search_20250305"}]})
    assert fakes["bed"].seen == []
