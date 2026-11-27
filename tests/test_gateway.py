"""The gateway fails over between upstreams before the client sees a 429."""

from __future__ import annotations

import http.client
import itertools
import json
import threading
import time
from collections.abc import Callable, Iterator
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
        self.queue: list[tuple[int, bytes]] = []  # answers to give first, one per request
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
                status, answer = outer.queue.pop(0) if outer.queue else (outer.status, outer.body)
                self.send_response(status)
                for k, v in outer.headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in outer.headers:
                    self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

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


def test_the_status_endpoint_says_which_upstream_served_each_session(fakes, run) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(settings(fakes), keys=gateway.Keys())
    port = run(gw)
    turn = {"model": "claude-opus-5-5", "tools": [{"name": "Read"}]}
    auth = ("Authorization", "Bearer oauth")
    post(port, turn, headers=[auth, ("X-Claude-Code-Session-Id", "abc-123")])
    post(port, {"model": "claude-opus-5-5"}, headers=[auth, ("X-Claude-Code-Session-Id", "side")])
    post(port, turn, headers=[auth, ("X-Claude-Code-Session-Id", "not an id")])
    post(port, turn, headers=[auth])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/_cabildo/status")
    assert json.loads(conn.getresponse().read())["sessions"] == {"abc-123": "bed"}


def test_the_gateway_forgets_the_oldest_sessions(fakes, monkeypatch) -> None:
    monkeypatch.setattr(gateway, "MAX_CONVERSATIONS", 2)
    gw = gateway.Gateway(settings(fakes))
    for sid in ("a", "b", "a", "c"):
        gw.note_session(sid, "bed")
    assert list(gw.sessions()) == ["a", "c"]


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
        {"region": "eu-central-1", "key": "nope", "models": {"a": "b"}},
        {"region": "eu-central-1", "key": "env:K"},
    ],
)
def test_invoke_needs_a_region_or_url_a_good_key_and_models(entry: dict) -> None:
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


@pytest.mark.parametrize(
    ("sent", "expected", "logged"),
    [
        (
            {"thinking": {"type": "enabled", "budget_tokens": 1024}},
            {"thinking": {"type": "adaptive"}},
            "thinking enabled -> adaptive",
        ),
        (
            {"context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]}},
            {},
            "dropped context_management",
        ),
        ({"service_tier": "auto"}, {}, "dropped service_tier"),
        ({"temperature": 1, "top_p": 0.9}, {"temperature": 1}, "dropped top_p"),
    ],
)
def test_invoke_rewrites_the_fields_bedrock_refuses_and_logs_it(
    fakes, run, sent: dict, expected: dict, logged: str
) -> None:
    fakes["sub"].status = 429
    lines: list[str] = []
    gw = gateway.Gateway(invoke(fakes), keys=gateway.Keys(), log=lines.append)
    port = run(gw)
    body = {"model": "claude-opus-5-5", "max_tokens": 5, "messages": [], **sent}
    status, _, _ = post(port, body, headers=[("Authorization", "Bearer oauth")])
    assert status == 200
    seen = json.loads(fakes["bed"].seen[0]["body"])
    seen.pop("anthropic_version")
    assert seen == {"max_tokens": 5, "messages": [], **expected}
    assert any(logged in line and "eu" in line for line in lines)


def test_invoke_keeps_top_p_alone_and_adaptive_thinking(fakes, run) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(invoke(fakes), keys=gateway.Keys())
    port = run(gw)
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 5,
        "messages": [],
        "top_p": 0.9,
        "thinking": {"type": "adaptive"},
    }
    post(port, body, headers=[("Authorization", "Bearer oauth")])
    seen = json.loads(fakes["bed"].seen[0]["body"])
    assert seen["top_p"] == 0.9 and seen["thinking"] == {"type": "adaptive"}


def test_drop_fields_removes_what_a_key_upstream_refuses(fakes, run) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(
        settings(fakes, bed={"drop_fields": ["context_management"]}), keys=gateway.Keys()
    )
    port = run(gw)
    body = {"model": "claude-opus-5-5", "context_management": {"edits": []}, "keep": 1}
    post(port, body)
    assert json.loads(fakes["bed"].seen[0]["body"]) == {"model": "claude-opus-5-5", "keep": 1}
    assert "context_management" in json.loads(fakes["sub"].seen[0]["body"])


def test_drop_fields_is_checked() -> None:
    with pytest.raises(gateway.GatewayError, match="list of field names"):
        gateway.parse(
            {
                "upstream": {
                    "k": {"kind": "key", "base_url": "http://x", "key": "env:K", "drop_fields": "a"}
                }
            }
        )
    with pytest.raises(gateway.GatewayError, match="passthrough"):
        gateway.parse(
            {
                "upstream": {
                    "p": {"kind": "passthrough", "base_url": "http://x", "drop_fields": ["a"]}
                }
            }
        )


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


# --- thinking signatures across upstreams (#61) --------------------------------------------------

SIGNED = {"type": "thinking", "thinking": "hm", "signature": "sig-from-one-upstream"}


def history(*, system: str = "sys", first: str = "hello", **extra: Any) -> dict:
    """A request with one earlier assistant turn that holds a signed thinking block."""
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 5,
        "system": [{"type": "text", "text": system}],
        "messages": [
            {"role": "user", "content": first},
            {"role": "assistant", "content": [SIGNED, {"type": "text", "text": "hi"}]},
            {"role": "user", "content": "and now?"},
        ],
        **extra,
    }


def test_strip_thinking_keeps_everything_else() -> None:
    data = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    SIGNED,
                    {"type": "redacted_thinking", "data": "x"},
                    {"type": "tool_use", "id": "t", "name": "n", "input": {}},
                ],
            },
            {"role": "assistant", "content": [SIGNED]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
        ]
    }
    assert gateway.strip_thinking(data) == 3
    assert [m["role"] for m in data["messages"]] == ["user", "assistant", "user"]
    assert data["messages"][1]["content"] == [
        {"type": "tool_use", "id": "t", "name": "n", "input": {}}
    ]
    assert gateway.strip_thinking({"messages": "nope"}) == 0


def test_a_conversation_key_follows_the_first_system_block_and_first_user_message() -> None:
    key = gateway.conversation_key(history())
    assert key == gateway.conversation_key(history())
    assert key != gateway.conversation_key(history(first="another"))
    assert key != gateway.conversation_key(history(system="other"))
    assert gateway.conversation_key({"messages": []}) is None


def test_failing_over_removes_the_thinking_blocks_another_upstream_issued(fakes, run) -> None:
    lines: list[str] = []
    gw = gateway.Gateway(settings(fakes), keys=gateway.Keys(), log=lines.append)
    port = run(gw)
    assert post(port, history())[0] == 200  # the subscription serves it and issues the blocks
    fakes["sub"].status = 429
    assert post(port, history())[0] == 200  # bedrock takes over
    sent = json.loads(fakes["bed"].seen[0]["body"])
    assert all(m["content"] != [SIGNED] for m in sent["messages"])
    assert b"signature" not in fakes["bed"].seen[0]["body"]
    assert any("dropped 1 thinking block" in line for line in lines)
    assert b"signature" in fakes["sub"].seen[0]["body"], "the first upstream got it unchanged"
    # The client keeps sending the old blocks: they stay out, whichever upstream answers.
    fakes["sub"].status = 200
    gw.router.restore(({}, {}, {}))
    gw.router = gateway.Router(gw.settings)
    post(port, history(), headers=[("Authorization", "Bearer oauth")])
    assert b"signature" not in fakes["sub"].seen[-1]["body"]


def test_an_upstream_that_refuses_a_signature_gets_the_request_again_without_blocks(
    fakes, run
) -> None:
    lines: list[str] = []
    fakes["sub"].queue = [
        (
            400,
            b'{"type": "error", "error": {"type": "invalid_request_error", '
            b'"message": "messages.1.content.0: Invalid `signature` in `thinking` block"}}',
        )
    ]
    gw = gateway.Gateway(settings(fakes), keys=gateway.Keys(), log=lines.append)
    port = run(gw)
    status, _, _ = post(port, history())
    assert status == 200
    first, second = fakes["sub"].seen
    assert b"signature" in first["body"] and b"signature" not in second["body"]
    assert any("retrying without" in line for line in lines)
    assert fakes["bed"].seen == [], "the retry went to the same upstream"
    assert gw.issued(gateway.conversation_key(history())) == gateway.Conversation("sub", True)


def test_a_400_about_something_else_is_not_retried(fakes, run) -> None:
    fakes["sub"].queue = [(400, b'{"error": {"message": "max_tokens is too large"}}')]
    port = run(gateway.Gateway(settings(fakes), keys=gateway.Keys()))
    assert post(port, history())[0] == 400
    assert len(fakes["sub"].seen) == 1


def test_manual_thinking_is_turned_off_when_blocks_are_removed(fakes, run) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(settings(fakes), keys=gateway.Keys(), log=lambda _m: None)
    port = run(gw)
    body = history(thinking={"type": "enabled", "budget_tokens": 1024})
    gw.remember(gateway.conversation_key(body), "sub", tainted=False)
    assert post(port, body)[0] == 200
    sent = json.loads(fakes["bed"].seen[0]["body"])
    assert "thinking" not in sent


def test_invoke_drops_a_temperature_other_than_1_while_thinking_is_on(fakes, run) -> None:
    fakes["sub"].status = 429
    port = run(gateway.Gateway(invoke(fakes), keys=gateway.Keys()))
    post(
        port,
        history(thinking={"type": "enabled", "budget_tokens": 1024}, temperature=0.3),
        headers=[("Authorization", "Bearer oauth")],
    )
    sent = json.loads(fakes["bed"].seen[0]["body"])
    assert sent["thinking"] == {"type": "adaptive"} and "temperature" not in sent
    post(port, history(thinking={"type": "adaptive"}, temperature=1))
    assert json.loads(fakes["bed"].seen[1]["body"])["temperature"] == 1


# --- the upstream's error message in the log ----------------------------------------------------


def logged(lines: list[str], wanted: Callable[[str], bool]) -> bool:
    """Whether a log line matches, waiting a moment: the gateway logs after it has answered."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(wanted(line) for line in list(lines)):
            return True
        time.sleep(0.02)
    return False


def test_error_text_reads_the_message_and_never_the_whole_body() -> None:
    anthropic = b'{"type": "error", "error": {"type": "x", "message": "prompt is\\n too long"}}'
    assert gateway.error_text(anthropic) == "prompt is too long"
    assert gateway.error_text(b'{"message": "bedrock says no"}') == "bedrock says no"
    assert gateway.error_text(b"<html>gateway timeout</html>") == ""
    assert gateway.error_text(b'{"error": {"code": 7}}') == ""
    long = json.dumps({"message": "word " * 100}).encode()
    assert len(gateway.error_text(long)) == 200
    leaked = json.dumps({"message": "bad key sk-" + "a1" * 30 + " refused"}).encode()
    assert gateway.error_text(leaked) == "bad key <redacted> refused"


def test_a_4xx_from_an_upstream_is_logged_with_its_message(fakes, run) -> None:
    fakes["sub"].status = 429
    fakes["bed"].status = 400
    fakes["bed"].body = b'{"type": "error", "error": {"message": "prompt is too long: 1 > 0"}}'
    lines: list[str] = []
    port = run(gateway.Gateway(settings(fakes), keys=gateway.Keys(), log=lines.append))
    assert post(port)[0] == 400
    assert logged(
        lines,
        lambda line: (
            line.startswith("bed claude-opus-5-5 400")
            and line.endswith(": prompt is too long: 1 > 0")
        ),
    )


def test_a_set_aside_is_logged_with_the_message_and_a_200_has_none(fakes, run) -> None:
    fakes["sub"].status = 429
    fakes["sub"].body = b'{"error": {"message": "weekly limit reached"}}'
    lines: list[str] = []
    port = run(gateway.Gateway(settings(fakes), keys=gateway.Keys(), log=lines.append))
    assert post(port)[0] == 200
    assert logged(lines, lambda x: "set aside" in x and x.endswith(": weekly limit reached"))
    assert logged(lines, lambda x: x.startswith("bed claude-opus-5-5 200") and ": " not in x)


def test_a_500_is_logged_with_its_message_and_still_reaches_the_client(fakes, run) -> None:
    fakes["sub"].status = 500
    fakes["sub"].body = b'{"error": {"message": "internal trouble"}}'
    lines: list[str] = []
    port = run(gateway.Gateway(settings(fakes), keys=gateway.Keys(), log=lines.append))
    status, _, out = post(port)
    assert status == 500 and b"internal trouble" in out
    assert logged(
        lines,
        lambda x: x.startswith("sub claude-opus-5-5 500") and x.endswith(": internal trouble"),
    )


# --- the clearing of old tool results that Bedrock's Invoke API does not do ---------------------

BIG = "result " * 400  # about 930 tokens by the gateway's estimate


def tool_history(count: int, **edit: Any) -> dict:
    """A conversation of ``count`` tool calls, each with a large result."""
    messages: list[dict] = [{"role": "user", "content": "go"}]
    for n in range(count):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"t{n}",
                        "name": "Read" if n else "Bash",
                        "input": {"n": n},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": BIG}],
            }
        )
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 5,
        "messages": messages,
        "context_management": {"edits": [{"type": "clear_tool_uses_20250919", **edit}]},
    }


def cleared(data: dict) -> list[bool]:
    """For each tool result of ``data``, whether it was replaced by the placeholder."""
    return [
        b["content"] == gateway._CLEARED
        for m in data["messages"]
        if isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == "tool_result"
    ]


def test_old_tool_results_are_cleared_once_the_trigger_is_passed_and_the_last_three_stay() -> None:
    data = tool_history(6, trigger={"type": "input_tokens", "value": 2000})
    notes = gateway.apply_context_management(data)
    assert cleared(data) == [True, True, True, False, False, False]
    assert notes and notes[0].startswith("cleared 3 old tool result(s)")
    results = [
        m["content"][0] for m in data["messages"] if m["role"] == "user" and m["content"] != "go"
    ]
    assert [r["tool_use_id"] for r in results] == [f"t{n}" for n in range(6)]


def test_nothing_is_cleared_below_the_trigger_and_the_default_is_100k_tokens() -> None:
    small = tool_history(6, trigger={"type": "input_tokens", "value": 50_000})
    assert gateway.apply_context_management(small) == [] and not any(cleared(small))
    default = tool_history(6)
    assert gateway.apply_context_management(default) == []  # about 5,600 tokens: far below 100k


def test_keep_exclude_and_clear_tool_inputs_are_respected() -> None:
    data = tool_history(
        5,
        trigger={"type": "input_tokens", "value": 100},
        keep={"type": "tool_uses", "value": 1},
        exclude_tools=["Bash"],
        clear_tool_inputs=True,
    )
    gateway.apply_context_management(data)
    assert cleared(data) == [False, True, True, True, False]  # t0 is Bash, t4 is the one kept
    uses = [b for m in data["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert [u["input"] for u in uses] == [{"n": 0}, {}, {}, {}, {"n": 4}]


def test_a_trigger_in_tool_uses_counts_calls_not_tokens() -> None:
    few = tool_history(
        3, trigger={"type": "tool_uses", "value": 3}, keep={"type": "tool_uses", "value": 1}
    )
    assert gateway.apply_context_management(few) == []
    many = tool_history(
        4, trigger={"type": "tool_uses", "value": 3}, keep={"type": "tool_uses", "value": 1}
    )
    assert gateway.apply_context_management(many) and cleared(many) == [True, True, True, False]


def test_clear_at_least_stops_a_clearing_that_would_free_too_little() -> None:
    data = tool_history(
        5,
        trigger={"type": "input_tokens", "value": 100},
        clear_at_least={"type": "input_tokens", "value": 50_000},
    )
    assert gateway.apply_context_management(data) == [] and not any(cleared(data))


def test_clearing_twice_clears_nothing_more() -> None:
    data = tool_history(6, trigger={"type": "input_tokens", "value": 100})
    gateway.apply_context_management(data)
    before = json.dumps(data)
    gateway.apply_context_management(data)
    assert json.dumps(data) == before


def test_results_pile_up_again_after_a_clearing_until_the_trigger_is_passed_again() -> None:
    seen: list[list[bool]] = []
    for count in range(1, 16):
        data = tool_history(count, trigger={"type": "input_tokens", "value": 6000})
        gateway.apply_context_management(data)
        seen.append(cleared(data))
    for before, after in itertools.pairwise(seen):
        assert after[: len(before) - 1] == before[:-1] or sum(after) > sum(before)
        assert [a for b, a in zip(before, after, strict=False) if b] == [True] * sum(before)
    totals = [sum(c) for c in seen]
    assert max(len(c) - sum(c) for c in seen) > 3, "more than the last three are kept"
    assert 1 < len(set(totals)) <= 5, "a few clearings, not one for each new result"


def test_the_results_the_model_has_not_read_yet_are_never_cleared() -> None:
    data = tool_history(2, trigger={"type": "input_tokens", "value": 100})
    ids = [f"p{n}" for n in range(5)]
    data["messages"] += [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": i, "name": "Read", "input": {}} for i in ids],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": i, "content": BIG} for i in ids],
        },
    ]
    gateway.apply_context_management(data)
    assert cleared(data) == [True, True, False, False, False, False, False]


def test_a_request_without_context_management_or_messages_is_left_alone() -> None:
    assert gateway.apply_context_management({"messages": []}) == []
    assert (
        gateway.apply_context_management({"context_management": {"edits": [{"type": "other"}]}})
        == []
    )


def test_invoke_clears_old_tool_results_and_drops_the_field(fakes, run) -> None:
    fakes["sub"].status = 429
    lines: list[str] = []
    port = run(gateway.Gateway(invoke(fakes), keys=gateway.Keys(), log=lines.append))
    body = tool_history(6, trigger={"type": "input_tokens", "value": 2000})
    assert post(port, body, headers=[("Authorization", "Bearer oauth")])[0] == 200
    sent = json.loads(fakes["bed"].seen[0]["body"])
    assert "context_management" not in sent
    assert cleared(sent) == [True, True, True, False, False, False]
    assert any(
        "cleared 3 old tool result(s)" in line and "dropped context_management" in line
        for line in lines
    )


def test_the_first_upstream_still_gets_the_field_and_the_full_history(fakes, run) -> None:
    port = run(gateway.Gateway(invoke(fakes), keys=gateway.Keys()))
    body = tool_history(6, trigger={"type": "input_tokens", "value": 2000})
    post(port, body, headers=[("Authorization", "Bearer oauth")])
    sent = json.loads(fakes["sub"].seen[0]["body"])
    assert "context_management" in sent and not any(cleared(sent))
