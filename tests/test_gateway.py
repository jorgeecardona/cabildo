"""The gateway fails over between upstreams before the client sees a 429 (#57)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from cabildo import gateway, onepassword


class Fake:
    """An upstream that records what it got and answers from a script."""

    def __init__(
        self, status: int = 200, body: bytes = b'{"ok": true}', headers: dict | None = None
    ):
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
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": self.rfile.read(n),
                    }
                )
                self.send_response(outer.status)
                for k, v in outer.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(outer.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fakes() -> Iterator[dict[str, Fake]]:
    made = {"sub": Fake(), "bed": Fake()}
    yield made
    for f in made.values():
        f.close()


def settings(fakes: dict[str, Fake], policy: str = "failover", **bed: Any) -> gateway.Settings:
    return gateway.parse(
        {
            "policy": policy,
            "upstream": {
                "sub": {"kind": "passthrough", "base_url": fakes["sub"].url},
                "bed": {
                    "kind": "key",
                    "base_url": fakes["bed"].url + "/anthropic",
                    "key": "env:BED_KEY",
                    **bed,
                },
            },
        }
    )


def serve(gw: gateway.Gateway) -> tuple[gateway.ThreadingHTTPServer, int]:
    server = gateway.make_server(gw, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_port


def post(
    port: int, body: dict | None = None, headers: dict | None = None
) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST",
        "/v1/messages",
        json.dumps(body or {"model": "claude-opus-5-5"}),
        {"Authorization": "Bearer oauth-token", "anthropic-beta": "oauth-2025", **(headers or {})},
    )
    r = conn.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    conn.close()
    return out


@pytest.fixture(autouse=True)
def bed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BED_KEY", "bedrock-secret")


def test_the_first_upstream_gets_the_request_with_the_clients_own_login(fakes) -> None:
    server, port = serve(gateway.Gateway(settings(fakes)))
    try:
        status, _h, body = post(port)
    finally:
        server.shutdown()
    assert (status, json.loads(body)) == (200, {"ok": True})
    seen = fakes["sub"].seen[0]
    assert seen["headers"]["authorization"] == "Bearer oauth-token"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025"
    assert fakes["bed"].seen == []


def test_a_429_moves_the_request_to_the_next_upstream_with_its_own_key(fakes) -> None:
    fakes["sub"].status, fakes["sub"].headers = 429, {"retry-after": "120"}
    gw = gateway.Gateway(settings(fakes))
    server, port = serve(gw)
    try:
        status, _h, _b = post(port)
        again = post(port)[0]
    finally:
        server.shutdown()
    assert status == again == 200
    bed = fakes["bed"].seen[0]
    assert bed["path"] == "/anthropic/v1/messages"
    assert bed["headers"]["x-api-key"] == "bedrock-secret"
    assert bed["headers"]["authorization"] == "Bearer bedrock-secret"  # not the client's token
    assert len(fakes["sub"].seen) == 1, "the second request skips the upstream that was set aside"
    rows = {r["name"]: r for r in gw.router.status()}
    assert not rows["sub"]["ready"] and rows["sub"]["set_aside_s"] > 100
    assert rows["bed"]["ready"]


def test_when_every_upstream_is_exhausted_the_client_gets_the_real_answer(fakes) -> None:
    for f in fakes.values():
        f.status, f.body = 429, b'{"error": "limit"}'
    server, port = serve(gateway.Gateway(settings(fakes)))
    try:
        status, _h, body = post(port)
    finally:
        server.shutdown()
    assert status == 429 and b"limit" in body


def test_a_400_that_is_not_about_credit_goes_to_the_client_and_nothing_is_set_aside(fakes) -> None:
    fakes["sub"].status, fakes["sub"].body = 400, b'{"error": "bad request"}'
    gw = gateway.Gateway(settings(fakes))
    server, port = serve(gw)
    try:
        status = post(port)[0]
    finally:
        server.shutdown()
    assert status == 400
    assert fakes["bed"].seen == []
    assert all(r["ready"] for r in gw.router.status())


def test_a_low_credit_400_fails_over(fakes) -> None:
    fakes["sub"].status = 400
    fakes["sub"].body = b'{"error": {"message": "Your credit balance is too low"}}'
    server, port = serve(gateway.Gateway(settings(fakes)))
    try:
        status = post(port)[0]
    finally:
        server.shutdown()
    assert status == 200 and len(fakes["bed"].seen) == 1


def test_models_are_renamed_for_the_upstream_that_maps_them(fakes) -> None:
    fakes["sub"].status = 429
    gw = gateway.Gateway(settings(fakes, models={"claude-opus-5-5": "anthropic.claude-opus-5-5"}))
    server, port = serve(gw)
    try:
        post(port, {"model": "claude-opus-5-5", "max_tokens": 5})
    finally:
        server.shutdown()
    assert json.loads(fakes["bed"].seen[0]["body"]) == {
        "model": "anthropic.claude-opus-5-5",
        "max_tokens": 5,
    }


def test_round_robin_alternates_the_first_choice(fakes) -> None:
    server, port = serve(gateway.Gateway(settings(fakes, policy="round_robin")))
    try:
        for _ in range(4):
            post(port)
    finally:
        server.shutdown()
    assert len(fakes["sub"].seen) == 2 and len(fakes["bed"].seen) == 2


def test_an_unreadable_key_sets_that_upstream_aside_and_the_next_serves(
    fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BED_KEY")
    fakes["sub"].status = 429
    server, port = serve(gateway.Gateway(settings(fakes)))
    try:
        status = post(port)[0]
    finally:
        server.shutdown()
    assert status == 429  # nothing could serve it: the real 429 is returned, no secret in sight


def test_an_unreachable_upstream_is_skipped(fakes) -> None:
    dead = settings(fakes)
    dead = gateway.Settings(
        dead.port,
        dead.policy,
        (gateway.Upstream("gone", "passthrough", "http://127.0.0.1:9"), *dead.upstreams),
    )
    server, port = serve(gateway.Gateway(dead))
    try:
        status = post(port)[0]
    finally:
        server.shutdown()
    assert status == 200


def test_the_status_endpoint_lists_the_upstreams(fakes) -> None:
    server, port = serve(gateway.Gateway(settings(fakes)))
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/_cabildo/status")
        data = json.loads(conn.getresponse().read())
    finally:
        server.shutdown()
    assert [u["name"] for u in data["upstreams"]] == ["sub", "bed"]


def test_a_streamed_body_arrives_whole(fakes) -> None:
    events = b"".join(b"event: x\ndata: %d\n\n" % i for i in range(200))
    fakes["sub"].body, fakes["sub"].headers = events, {"Content-Type": "text/event-stream"}
    server, port = serve(gateway.Gateway(settings(fakes)))
    try:
        status, headers, body = post(port)
    finally:
        server.shutdown()
    assert status == 200 and body == events
    assert headers["Content-Type"] == "text/event-stream"


# --- config and keys ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        {},
        {"upstream": {"a": {"kind": "nope", "base_url": "http://x"}}},
        {"upstream": {"a": {"kind": "key", "base_url": "http://x"}}},
        {"upstream": {"a": {"kind": "passthrough", "base_url": "ftp://x"}}},
        {"policy": "random", "upstream": {"a": {"kind": "passthrough", "base_url": "http://x"}}},
        {"port": 99999, "upstream": {"a": {"kind": "passthrough", "base_url": "http://x"}}},
    ],
)
def test_a_bad_config_is_refused_with_a_reason(table: dict) -> None:
    with pytest.raises(gateway.GatewayError):
        gateway.parse(table)


def test_keys_read_env_file_and_1password(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def read_op(ref: str) -> onepassword.Secret:
        calls.append(ref)
        return onepassword.Secret("from-op")

    keys = gateway.Keys(read_op)
    monkeypatch.setenv("K", "from-env")
    (tmp_path / "k").write_text("from-file\n")
    assert keys.get("env:K") == "from-env"
    assert keys.get(f"file:{tmp_path / 'k'}") == "from-file"
    assert keys.get("op://V/I/f") == keys.get("op://V/I/f") == "from-op"
    assert calls == ["op://V/I/f"], "a 1Password key is read once"
    keys.forget("op://V/I/f")
    keys.get("op://V/I/f")
    assert len(calls) == 2
    for bad in ("env:NOPE_UNSET", "plain", f"file:{tmp_path / 'missing'}"):
        with pytest.raises(gateway.GatewayError):
            keys.get(bad)


def test_an_error_never_holds_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K", "")
    with pytest.raises(gateway.GatewayError) as err:
        gateway.Keys().get("env:K")
    assert "env:" in str(err.value)


def test_retry_after_sets_the_cooldown() -> None:
    assert gateway.classify(429, {"Retry-After": "90"}, b"").cooldown_s == 90  # type: ignore[union-attr]
    assert gateway.classify(429, {}, b"").cooldown_s == gateway.DEFAULT_COOLDOWN_S  # type: ignore[union-attr]
    assert gateway.classify(500, {}, b"") is None
    assert gateway.classify(529, {}, b"") is None


def test_reconfigure_keeps_an_upstream_set_aside_and_drops_one_that_is_gone(fakes) -> None:
    gw = gateway.Gateway(settings(fakes))
    gw.router.set_aside(gw.settings.upstreams[0], gateway.Exhausted("limit", 100))
    only_sub = gateway.parse(
        {"upstream": {"sub": {"kind": "passthrough", "base_url": fakes["sub"].url}}}
    )
    gw.reconfigure(only_sub)
    rows = gw.router.status()
    assert [r["name"] for r in rows] == ["sub"]
    assert not rows[0]["ready"]
