"""A local gateway for Claude Code that fails over between upstreams.

Claude Code sends every model request to ``ANTHROPIC_BASE_URL``. Point it at this server once
(``http://127.0.0.1:8787``), and the choice of upstream moves out of the session: when your
subscription runs out, the next request goes to an API key or to Amazon Bedrock, with no restart.

    [gateway]
    port   = 8787
    policy = "failover"            # or "round_robin"

    [gateway.upstream.subscription]
    kind     = "passthrough"       # forwards the client's own login to Anthropic
    base_url = "https://api.anthropic.com"

    [gateway.upstream.bedrock]
    kind     = "key"
    base_url = "https://bedrock-mantle.us-east-1.api.aws/anthropic"
    key      = "env:AWS_BEARER_TOKEN_BEDROCK"      # or op://..., or file:/path
    models   = { "claude-opus-5-5" = "anthropic.claude-opus-5-5" }

An upstream that answers 429 (or 402, or an auth error) is set aside until its retry time, and
the same request goes to the next one before the client sees anything. Only the machine-wide
``~/.cabildo/config.toml`` is read. The server listens on 127.0.0.1 and never forwards a request
to a host the config does not name.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import onepassword

DEFAULT_PORT = 8787
DEFAULT_COOLDOWN_S = 300.0
AUTH_COOLDOWN_S = 600.0
BILLING_COOLDOWN_S = 3600.0
_BODY_PEEK = 65536  # how much of an error body is read to tell "out of credit" from a bad request
_CHUNK = 65536
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    }
)
_AUTH_HEADERS = frozenset({"authorization", "x-api-key"})
_KINDS = ("passthrough", "key")
_POLICIES = ("failover", "round_robin")


class GatewayError(ValueError):
    """The ``[gateway]`` config is not usable. The message never holds a secret."""


@dataclass(frozen=True)
class Upstream:
    """One place a request can go."""

    name: str
    kind: str  # "passthrough": the client's own credential; "key": ours, from ``key``
    base_url: str
    key: str | None = None  # "env:NAME", "file:/path" or "op://..."
    auth: str = "both"  # how a key is sent: "x-api-key", "bearer" or "both"
    models: Mapping[str, str] = field(default_factory=dict)  # client model id -> this upstream's


@dataclass(frozen=True)
class Settings:
    """The parsed ``[gateway]`` table."""

    port: int
    policy: str
    upstreams: tuple[Upstream, ...]


@dataclass(frozen=True)
class Exhausted:
    """Why an answer means "try another upstream", and for how long to leave this one alone."""

    reason: str
    cooldown_s: float


def parse(table: Mapping[str, Any]) -> Settings:
    """The ``Settings`` for a ``[gateway]`` table, upstreams in the order written.

    Raises:
        GatewayError: on an unknown kind or policy, a ``key`` upstream with no key, or a
            ``base_url`` that is not http(s) with a host.
    """
    policy = str(table.get("policy", "failover"))
    if policy not in _POLICIES:
        msg = f"policy must be one of {', '.join(_POLICIES)}"
        raise GatewayError(msg)
    raw = table.get("upstream")
    found: list[Upstream] = []
    for name, entry in raw.items() if isinstance(raw, Mapping) else ():
        if not isinstance(entry, Mapping):
            msg = f"upstream {name}: must be a table"
            raise GatewayError(msg)
        kind = str(entry.get("kind", ""))
        if kind not in _KINDS:
            msg = f"upstream {name}: kind must be one of {', '.join(_KINDS)}"
            raise GatewayError(msg)
        base = str(entry.get("base_url", "")).rstrip("/")
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            msg = f"upstream {name}: base_url must be http(s)://host[/path]"
            raise GatewayError(msg)
        key = entry.get("key")
        if kind == "key" and not (isinstance(key, str) and key):
            msg = f"upstream {name}: a key upstream needs key"
            raise GatewayError(msg)
        auth = str(entry.get("auth", "both"))
        if auth not in ("x-api-key", "bearer", "both"):
            msg = f"upstream {name}: auth must be x-api-key, bearer or both"
            raise GatewayError(msg)
        models = entry.get("models")
        found.append(
            Upstream(
                name=name,
                kind=kind,
                base_url=base,
                key=key if isinstance(key, str) else None,
                auth=auth,
                models={str(k): str(v) for k, v in models.items()}
                if isinstance(models, Mapping)
                else {},
            )
        )
    if not found:
        msg = "no [gateway.upstream.<name>] tables"
        raise GatewayError(msg)
    port = table.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or not 0 <= port <= 65535:
        msg = "port must be 0-65535"
        raise GatewayError(msg)
    return Settings(port=port, policy=policy, upstreams=tuple(found))


def classify(status: int, headers: Mapping[str, str], body: bytes) -> Exhausted | None:
    """Whether an upstream's answer means it cannot serve requests for a while.

    429 is a rate or usage limit (``retry-after`` says when it lifts). 402 and a 400 that says the
    credit balance is too low are billing. 401 and 403 are a key that does not work. Anything
    else, a 5xx included, is the upstream's own answer and goes to the client unchanged.
    """
    low = {k.lower(): v for k, v in headers.items()}
    text = body[:_BODY_PEEK].decode("utf-8", "replace").lower()
    if status == 429:
        try:
            wait = float(low.get("retry-after", ""))
        except ValueError:
            wait = DEFAULT_COOLDOWN_S
        return Exhausted("rate or usage limit", max(wait, 1.0))
    if status == 402 or (status == 400 and "credit balance" in text):
        return Exhausted("out of credit", BILLING_COOLDOWN_S)
    if status in (401, 403):
        return Exhausted("credential refused", AUTH_COOLDOWN_S)
    return None


class Router:
    """Which upstream gets the next request, and which are set aside.

    Thread-safe: the server handles each client on its own thread.
    """

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
        until: Mapping[str, tuple[float, str]] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._until: dict[str, tuple[float, str]] = dict(until or {})
        self._turn = 0

    def snapshot(self) -> dict[str, tuple[float, str]]:
        """Which upstreams are set aside, until when and why: carried over by a reload."""
        with self._lock:
            return dict(self._until)

    def order(self) -> list[Upstream]:
        """The upstreams to try, in order: those not set aside first, then the rest.

        ``failover`` keeps the order of the config. ``round_robin`` rotates the start.
        """
        with self._lock:
            now = self._clock()
            ups = list(self._settings.upstreams)
            if self._settings.policy == "round_robin" and ups:
                start = self._turn % len(ups)
                self._turn += 1
                ups = ups[start:] + ups[:start]
            ready = [u for u in ups if self._until.get(u.name, (0.0, ""))[0] <= now]
            return ready + [u for u in ups if u not in ready]

    def ready(self, up: Upstream) -> bool:
        """True when ``up`` is not set aside."""
        with self._lock:
            return self._until.get(up.name, (0.0, ""))[0] <= self._clock()

    def set_aside(self, up: Upstream, why: Exhausted) -> None:
        """Leave ``up`` out of the first choices for ``why.cooldown_s`` seconds."""
        with self._lock:
            self._until[up.name] = (self._clock() + why.cooldown_s, why.reason)

    def status(self) -> list[dict[str, Any]]:
        """Each upstream with whether it is ready, for ``/_cabildo/status``."""
        with self._lock:
            now = self._clock()
            rows: list[dict[str, Any]] = []
            for u in self._settings.upstreams:
                until, why = self._until.get(u.name, (0.0, ""))
                left = max(0.0, until - now)
                rows.append(
                    {
                        "name": u.name,
                        "kind": u.kind,
                        "ready": left == 0.0,
                        "set_aside_s": round(left),
                        "reason": why if left else "",
                    }
                )
            return rows


class Keys:
    """Resolve the ``key`` of an upstream: ``env:NAME``, ``file:/path`` or ``op://...``.

    A 1Password key is read once and kept in memory (``op`` can wait on a human); the others are
    read each time, so changing the file or the variable takes effect without a restart.
    """

    def __init__(self, read_op: Callable[[str], onepassword.Secret] = onepassword.read) -> None:
        self._read_op = read_op
        self._op: dict[str, str] = {}

    def get(self, ref: str) -> str:
        """The key for ``ref``.

        Raises:
            GatewayError: when it is empty, unset or unreadable. The message never holds it.
        """
        if ref.startswith("env:"):
            value = os.environ.get(ref[4:], "")
        elif ref.startswith("file:"):
            try:
                value = Path(ref[5:]).expanduser().read_text(encoding="utf-8").strip()
            except OSError as exc:
                msg = f"cannot read key file {ref[5:]}: {exc.strerror}"
                raise GatewayError(msg) from exc
        elif ref.startswith(onepassword.REF_PREFIX):
            if ref not in self._op:
                try:
                    self._op[ref] = self._read_op(ref).reveal()
                except onepassword.OpError as exc:
                    msg = f"cannot read the 1Password key: {exc}"
                    raise GatewayError(msg) from exc
            value = self._op[ref]
        else:
            msg = "key must start with env:, file: or op://"
            raise GatewayError(msg)
        if not value:
            msg = f"the key {ref.split(':', 1)[0]}:... is empty"
            raise GatewayError(msg)
        return value

    def forget(self, ref: str) -> None:
        """Drop a kept key, so the next request reads it again (after the upstream refused it)."""
        self._op.pop(ref, None)


def request_headers(up: Upstream, client: Mapping[str, str], keys: Keys) -> dict[str, str]:
    """The headers to send to ``up``: the client's, minus hop-by-hop, with ``up``'s credential.

    A passthrough upstream gets the client's own ``authorization`` and ``x-api-key``. A key
    upstream gets neither of them: the client's credential is for another upstream.

    Raises:
        GatewayError: when a key upstream's key cannot be read.
    """
    out = {
        k: v
        for k, v in client.items()
        if k.lower() not in _HOP_BY_HOP
        and (up.kind == "passthrough" or k.lower() not in _AUTH_HEADERS)
    }
    out["Accept-Encoding"] = "identity"
    if up.kind == "key" and up.key:
        secret = keys.get(up.key)
        if up.auth in ("x-api-key", "both"):
            out["x-api-key"] = secret
        if up.auth in ("bearer", "both"):
            out["Authorization"] = f"Bearer {secret}"
    return out


def request_body(up: Upstream, path: str, body: bytes) -> bytes:
    """``body`` with the ``model`` renamed for ``up`` when its ``models`` table maps it."""
    if not up.models or not body or not path.split("?", 1)[0].startswith("/v1/messages"):
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if isinstance(data, dict) and isinstance(data.get("model"), str) and data["model"] in up.models:
        data["model"] = up.models[data["model"]]
        return json.dumps(data).encode()
    return body


Connect = Callable[[Upstream, float], http.client.HTTPConnection]


def connect(up: Upstream, timeout: float) -> http.client.HTTPConnection:
    """An HTTP(S) connection to the host of ``up``."""
    parts = urlsplit(up.base_url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    return cls(parts.hostname or "", parts.port, timeout=timeout)


@dataclass
class Gateway:
    """The routing, key lookup and connection factory a server's handlers share."""

    settings: Settings
    router: Router = field(init=False)
    keys: Keys = field(default_factory=Keys)
    connect: Connect = connect
    timeout: float = 600.0
    log: Callable[[str], None] = lambda _m: None

    def __post_init__(self) -> None:
        self.router = Router(self.settings)

    def attempts(self) -> Sequence[Upstream]:
        """The upstreams to try for one request."""
        return self.router.order()

    def reconfigure(self, settings: Settings) -> None:
        """Use new settings for the next request. Requests in flight finish as they are, and an
        upstream that is set aside stays set aside if the new settings still name it.
        """
        names = {u.name for u in settings.upstreams}
        kept = {n: v for n, v in self.router.snapshot().items() if n in names}
        self.settings = settings
        self.router = Router(settings, until=kept)


def make_handler(gw: Gateway) -> type[BaseHTTPRequestHandler]:
    """The request handler class bound to ``gw``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # the body ends when the connection closes: streams as is

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/_cabildo/status":
                data = json.dumps({"upstreams": gw.router.status()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
                return
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        do_PUT = do_DELETE = do_PATCH = do_POST  # noqa: N815

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            client = dict(self.headers.items())
            order = list(gw.attempts())
            last: tuple[int, list[tuple[str, str]], bytes] | None = None
            for i, up in enumerate(order):
                more = i < len(order) - 1
                try:
                    headers = request_headers(up, client, gw.keys)
                except GatewayError as exc:
                    gw.log(f"gateway: {up.name}: {exc}")
                    gw.router.set_aside(up, Exhausted("no usable key", AUTH_COOLDOWN_S))
                    continue
                path = urlsplit(up.base_url).path + self.path
                try:
                    conn = gw.connect(up, gw.timeout)
                    conn.request(self.command, path, request_body(up, self.path, body), headers)
                    resp = conn.getresponse()
                except (OSError, http.client.HTTPException) as exc:
                    gw.log(f"gateway: {up.name}: {type(exc).__name__}: {exc}")
                    gw.router.set_aside(up, Exhausted("unreachable", 30.0))
                    continue
                verdict = None
                if resp.status in (400, 401, 402, 403, 429):
                    peek = resp.read(_BODY_PEEK)
                    verdict = classify(resp.status, dict(resp.getheaders()), peek)
                    if verdict is not None:
                        if resp.status in (401, 403) and up.key:
                            gw.keys.forget(up.key)
                        gw.router.set_aside(up, verdict)
                        gw.log(
                            f"gateway: {up.name} set aside {verdict.cooldown_s:.0f}s: {verdict.reason}"
                        )
                        last = (resp.status, resp.getheaders(), peek)
                        conn.close()
                        if more:
                            continue
                        break
                    last = (resp.status, resp.getheaders(), peek)
                    self._send(resp.status, resp.getheaders(), peek, None)
                    conn.close()
                    return
                self._send(resp.status, resp.getheaders(), b"", resp)
                conn.close()
                return
            if last is not None:
                self._send(last[0], last[1], last[2], None)
                return
            self._send(
                502,
                [("Content-Type", "application/json")],
                json.dumps(
                    {"type": "error", "error": {"type": "api_error", "message": "no upstream"}}
                ).encode(),
                None,
            )

        def _send(
            self,
            status: int,
            headers: list[tuple[str, str]],
            body: bytes,
            stream: http.client.HTTPResponse | None,
        ) -> None:
            try:
                self.send_response(status)
                for k, v in headers:
                    if k.lower() not in _HOP_BY_HOP:
                        self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)
                while stream is not None:
                    chunk = stream.read1(_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # the client went away

    return Handler


def make_server(gw: Gateway, port: int | None = None) -> ThreadingHTTPServer:
    """A server for ``gw`` on 127.0.0.1. ``port`` 0 picks a free one."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", gw.settings.port if port is None else port), make_handler(gw)
    )
    server.daemon_threads = True
    return server
