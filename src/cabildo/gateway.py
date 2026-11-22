"""A local gateway for Claude Code that fails over between upstreams.

Claude Code sends every model request to ``ANTHROPIC_BASE_URL``. Point it at this server once
(``http://127.0.0.1:8788``), and the choice of upstream moves out of the session: when your
subscription runs out, the next request goes to an API key or to Amazon Bedrock, with no restart.

    [gateway]
    port = 8788
    # token = "env:CABILDO_GATEWAY_TOKEN"   # optional: clients must send x-cabildo-gateway

    [gateway.upstream.subscription]
    kind     = "passthrough"       # forwards the client's own login to Anthropic
    base_url = "https://api.anthropic.com"

    [gateway.upstream.bedrock]
    kind         = "key"
    base_url     = "https://bedrock-mantle.us-east-1.api.aws/anthropic"
    key          = "env:AWS_BEARER_TOKEN_BEDROCK"      # or op://..., or file:/path
    server_tools = false           # Bedrock has no WebSearch and the like
    models       = { "claude-opus-5-5" = "anthropic.claude-opus-5-5" }

A Bedrock region with no Mantle endpoint uses ``kind = "bedrock-invoke"``: the gateway then calls
the Invoke API and turns its binary answer into server-sent events (docs/gateway.md).

The upstreams are tried in the order of the file. An upstream that answers 429 (or out of credit,
or a key that is refused) is set aside until its retry time, and the same request goes to the next
one before the client sees anything. Only the machine-wide ``~/.cabildo/config.toml`` is read. The
server listens on 127.0.0.1, refuses browser requests, and never forwards to a host the config
does not name.

The cabildo daemon runs it (``start_service``), like the LAN and WAN services, so there is one
service to install. On ``cabildo service reload`` the daemon stops accepting, waits for the open
requests, and passes the listening socket to the new process, so no client sees a refused
connection. ``cabildo gateway serve`` runs the same server in the foreground, for debugging.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from . import config, eventstream, onepassword, store

DEFAULT_PORT = 8788  # 8787 is the port of ``cabildo viewer``
RATE_COOLDOWN_KEY_S = 60.0
RATE_COOLDOWN_PASSTHROUGH_S = 300.0
OVERLOAD_COOLDOWN_S = 30.0
AUTH_COOLDOWN_S = 600.0
BILLING_COOLDOWN_S = 3600.0
UNREACHABLE_COOLDOWN_S = 30.0
MAX_BODY = 64 * 1024 * 1024
TOKEN_HEADER = "x-cabildo-gateway"  # noqa: S105
HANDOFF_ENV = "CABILDO_GATEWAY_FD"
DRAIN_S = 120.0  # how long a reload waits for open requests before it cuts them
BACKLOG = 128  # connections that queue while a reload swaps the process
MAX_CONVERSATIONS = 4096  # how many conversations the gateway remembers an issuer for
_ERROR_TEXT = 200  # characters of an upstream's error message that reach the log
_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{32,}")
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
    }
)
_NOT_FORWARDED = _HOP_BY_HOP | {"host", "content-length", "accept-encoding", TOKEN_HEADER}
_AUTH_HEADERS = frozenset({"authorization", "x-api-key"})
_KINDS = ("passthrough", "key", "bedrock-invoke")
_INVOKE = "bedrock-invoke"
_REGION = re.compile(r"[a-z]+(?:-[a-z0-9]+)+")
_ANTHROPIC_VERSION = "bedrock-2023-05-31"
_SIGNED = b'"signature"'  # every thinking block carries one
_THINKING = ("thinking", "redacted_thinking")
_SIGNATURE_ERROR = re.compile(rb"thinking.{0,12}(block|signature)|redacted_thinking", re.IGNORECASE)
_AUTHS = ("x-api-key", "bearer", "both")
_ID = re.compile(r"[A-Za-z0-9_.-]+")
_DURATION = re.compile(r"(\d+)([smhd])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class GatewayError(ValueError):
    """The ``[gateway]`` config or a request is not usable.

    The message never holds a secret.
    """


@dataclass(frozen=True)
class KeyRef:
    """Where a key is read from: an environment variable, a file, or a 1Password reference."""

    scheme: str  # "env", "file" or "op"
    value: str  # the variable name, the path, or the whole ``op://`` reference

    def __str__(self) -> str:
        return self.value if self.scheme == "op" else f"{self.scheme}:{self.value}"


def parse_key_ref(text: object) -> KeyRef:
    """The ``KeyRef`` for ``env:NAME``, ``file:/path`` or ``op://vault/item/field``.

    Raises:
        GatewayError: on anything else, or an empty name.
    """
    if isinstance(text, str):
        if text.startswith(onepassword.REF_PREFIX) and len(text) > len(onepassword.REF_PREFIX):
            return KeyRef("op", text)
        scheme, _, rest = text.partition(":")
        if scheme in ("env", "file") and rest:
            return KeyRef(scheme, rest)
    msg = "a key must be env:NAME, file:/path or op://vault/item/field"
    raise GatewayError(msg)


@dataclass(frozen=True)
class Upstream:
    """One place a request can go."""

    name: str
    kind: str  # "passthrough": the client's own login; "key": our key; "bedrock-invoke": Bedrock
    base_url: str
    key: KeyRef | None = None
    auth: str = "x-api-key"  # how a key is sent: "x-api-key", "bearer" or "both"
    models: Mapping[str, str] = field(default_factory=dict)  # client model id -> this upstream's
    drop_betas: tuple[str, ...] = ()  # ``anthropic-beta`` values this upstream cannot take
    drop_fields: tuple[str, ...] = ()  # top-level body fields this upstream refuses
    server_tools: bool = True  # False: a request with WebSearch and the like skips this upstream


@dataclass(frozen=True)
class Settings:
    """The parsed ``[gateway]`` table."""

    port: int
    upstreams: tuple[Upstream, ...]
    token: KeyRef | None = None


@dataclass(frozen=True)
class Exhausted:
    """Why an answer means "try another upstream", and for how long to leave this one alone."""

    reason: str
    cooldown_s: float
    scope: str = "upstream"  # "model": only for the model of the request


@dataclass(frozen=True)
class SetAside:
    """An upstream, or one model of it, that is left out of the first choices."""

    until: float  # on the router's clock
    reason: str
    model: str | None = None  # None: the whole upstream


@dataclass(frozen=True)
class UpstreamStatus:
    """One upstream as ``cabildo gateway status`` shows it."""

    name: str
    kind: str
    ready: bool
    set_aside_s: int
    reason: str
    requests: int
    failovers: int
    last_error: str
    forced: bool


@dataclass(frozen=True)
class Job:
    """What a request asks for, read once from its path and body."""

    path: str
    model: str | None = None
    server_tool: bool = False
    stream: bool = False
    conversation: str | None = None  # a key for the conversation: see ``conversation_key``


@dataclass(frozen=True)
class Force:
    """The upstream the user chose with ``cabildo gateway use``, until ``until`` (epoch seconds)."""

    name: str
    until: float


def parse(table: Mapping[str, Any]) -> Settings:
    """The ``Settings`` for a ``[gateway]`` table, upstreams in the order written.

    Raises:
        GatewayError: on an unknown kind or auth, a key upstream with no valid key, a bad
            ``base_url``, or a ``policy`` other than ``failover`` (the only one there is).
    """
    policy = table.get("policy", "failover")
    if policy != "failover":
        msg = "policy was removed: upstreams are tried in the order of the file"
        raise GatewayError(msg)
    raw = table.get("upstream")
    found: list[Upstream] = []
    for name, entry in raw.items() if isinstance(raw, Mapping) else ():
        if not _ID.fullmatch(str(name)):
            msg = f"upstream name {name!r}: use letters, digits, dot, dash or underscore"
            raise GatewayError(msg)
        if not isinstance(entry, Mapping):
            msg = f"upstream {name}: must be a table"
            raise GatewayError(msg)
        found.append(_upstream(str(name), entry))
    if not found:
        msg = "no [gateway.upstream.<name>] tables"
        raise GatewayError(msg)
    port = table.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        msg = "port must be 0-65535"
        raise GatewayError(msg)
    token = table.get("token")
    return Settings(
        port=port,
        upstreams=tuple(found),
        token=parse_key_ref(token) if token is not None else None,
    )


def _upstream(name: str, entry: Mapping[str, Any]) -> Upstream:
    kind = str(entry.get("kind", ""))
    if kind not in _KINDS:
        msg = f"upstream {name}: kind must be one of {', '.join(_KINDS)}"
        raise GatewayError(msg)
    base = str(entry.get("base_url", "")).rstrip("/")
    if kind == _INVOKE and not base:
        region = str(entry.get("region", ""))
        if not _REGION.fullmatch(region):
            msg = f"upstream {name}: give region (for example eu-central-1) or base_url"
            raise GatewayError(msg)
        base = f"https://bedrock-runtime.{region}.amazonaws.com"
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        msg = f"upstream {name}: base_url must be http(s)://host[/path]"
        raise GatewayError(msg)
    key = None
    if kind != "passthrough":
        try:
            key = parse_key_ref(entry.get("key"))
        except GatewayError as exc:
            msg = f"upstream {name}: {exc}"
            raise GatewayError(msg) from exc
    auth = str(entry.get("auth", "bearer" if kind == _INVOKE else "x-api-key"))
    if auth not in _AUTHS:
        msg = f"upstream {name}: auth must be one of {', '.join(_AUTHS)}"
        raise GatewayError(msg)
    models = entry.get("models")
    mapping = {str(k): str(v) for k, v in models.items()} if isinstance(models, Mapping) else {}
    if kind == _INVOKE and not mapping:
        msg = f"upstream {name}: models is required: Bedrock names models in its own way"
        raise GatewayError(msg)
    betas = entry.get("drop_betas")
    # An API key is not the claude.ai login: its upstream must not be sent the OAuth capability.
    drop = (
        tuple(str(b) for b in betas)
        if isinstance(betas, list)
        else (("oauth-*",) if kind != "passthrough" else ())
    )
    fields = entry.get("drop_fields", [])
    if not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields):
        msg = f"upstream {name}: drop_fields must be a list of field names"
        raise GatewayError(msg)
    if fields and kind == "passthrough":
        msg = f"upstream {name}: drop_fields does not apply to a passthrough upstream"
        raise GatewayError(msg)
    return Upstream(
        name=name,
        kind=kind,
        base_url=base,
        key=key,
        auth=auth,
        models=mapping,
        drop_betas=drop,
        drop_fields=tuple(fields),
        server_tools=entry.get("server_tools", kind != _INVOKE) is not False,
    )


def classify(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    *,
    kind: str = "key",
    epoch: Callable[[], float] = time.time,
) -> Exhausted | None:
    """Whether an upstream's answer means it cannot serve requests for a while.

    429 is a rate or usage limit for that model (``retry-after``, else the unified reset header,
    says when it lifts). 402 and a 400 that says the credit balance is too low are billing. 401
    is a key that does not work. 403 is a refusal for that model. 529 is overload. On a
    passthrough upstream a 401 or 403 is one session's login, so it is never an exhaustion.
    Anything else is the upstream's own answer and goes to the client unchanged.
    """
    low = {k.lower(): v for k, v in headers.items()}
    text = body[:_BODY_PEEK].decode("utf-8", "replace").lower()
    if status == 429:
        return Exhausted("rate or usage limit", _rate_wait(low, kind, epoch()), "model")
    if status == 402 or (status == 400 and "credit balance" in text):
        return Exhausted("out of credit", BILLING_COOLDOWN_S)
    if status == 529:
        return Exhausted("overloaded", OVERLOAD_COOLDOWN_S)
    if kind == "passthrough":
        return None
    if status == 401:
        return Exhausted("key refused", AUTH_COOLDOWN_S)
    if status == 403:
        return Exhausted("forbidden for this model", AUTH_COOLDOWN_S, "model")
    return None


def _rate_wait(low: Mapping[str, str], kind: str, now: float) -> float:
    """Seconds until a 429 lifts: ``retry-after``, else the unified reset time, else a default."""
    try:
        return max(float(low["retry-after"]), 1.0)
    except (KeyError, ValueError):
        pass
    try:
        return max(float(low["anthropic-ratelimit-unified-reset"]) - now, 1.0)
    except (KeyError, ValueError):
        return RATE_COOLDOWN_PASSTHROUGH_S if kind == "passthrough" else RATE_COOLDOWN_KEY_S


class Router:
    """Which upstream gets the next request, and which are set aside.

    Thread-safe: the server handles each client on its own thread.
    """

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
        aside: Mapping[tuple[str, str | None], SetAside] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._aside: dict[tuple[str, str | None], SetAside] = dict(aside or {})
        self._requests: dict[str, int] = {}
        self._failovers: dict[str, int] = {}
        self._last_error: dict[str, str] = {}

    def snapshot(self) -> dict[tuple[str, str | None], SetAside]:
        """Which upstreams are set aside, until when and why: carried over by a reload."""
        with self._lock:
            return dict(self._aside)

    def _blocked(self, name: str, model: str | None, now: float) -> SetAside | None:
        for key in ((name, None), (name, model)):
            hit = self._aside.get(key)
            if hit is not None and hit.until > now:
                return hit
        return None

    def order(self, model: str | None = None, forced: str | None = None) -> list[Upstream]:
        """The upstreams to try for ``model``, in order.

        The forced one comes first, then those not set aside in the order of the config, then
        the set-aside ones as a last resort.
        """
        with self._lock:
            now = self._clock()
            ups = list(self._settings.upstreams)
            first = [u for u in ups if u.name == forced]
            rest = [u for u in ups if u.name != forced]
            ready = [u for u in rest if self._blocked(u.name, model, now) is None]
            return first + ready + [u for u in rest if u not in ready]

    def set_aside(self, up: Upstream, why: Exhausted, model: str | None = None) -> None:
        """Leave ``up`` (for ``model`` when ``why.scope`` says so) out of the first choices."""
        with self._lock:
            key = (up.name, model if why.scope == "model" else None)
            self._aside[key] = SetAside(self._clock() + why.cooldown_s, why.reason, key[1])
            self._failovers[up.name] = self._failovers.get(up.name, 0) + 1
            self._last_error[up.name] = why.reason

    def served(self, up: Upstream) -> None:
        """Count a request that ``up`` answered (whatever the status)."""
        with self._lock:
            self._requests[up.name] = self._requests.get(up.name, 0) + 1

    def failed(self, up: Upstream, error: str) -> None:
        """Remember why the last request to ``up`` failed."""
        with self._lock:
            self._last_error[up.name] = error

    def status(self, forced: str | None = None) -> list[UpstreamStatus]:
        """Each upstream with whether it is ready, and its counts."""
        with self._lock:
            now = self._clock()
            rows: list[UpstreamStatus] = []
            for u in self._settings.upstreams:
                hits = [h for (n, _m), h in self._aside.items() if n == u.name and h.until > now]
                worst = max(hits, key=lambda h: h.until, default=None)
                left = max(0.0, worst.until - now) if worst else 0.0
                rows.append(
                    UpstreamStatus(
                        name=u.name,
                        kind=u.kind,
                        ready=not hits,
                        set_aside_s=round(left),
                        reason=(
                            f"{worst.reason}{f' ({worst.model})' if worst.model else ''}"
                            if worst
                            else ""
                        ),
                        requests=self._requests.get(u.name, 0),
                        failovers=self._failovers.get(u.name, 0),
                        last_error=self._last_error.get(u.name, ""),
                        forced=u.name == forced,
                    )
                )
            return rows

    def counts(self) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
        """The counters, carried over by a reload."""
        with self._lock:
            return dict(self._requests), dict(self._failovers), dict(self._last_error)

    def restore(self, counts: tuple[dict[str, int], dict[str, int], dict[str, str]]) -> None:
        """Take the counters of the router this one replaces."""
        names = {u.name for u in self._settings.upstreams}
        with self._lock:
            self._requests = {k: v for k, v in counts[0].items() if k in names}
            self._failovers = {k: v for k, v in counts[1].items() if k in names}
            self._last_error = {k: v for k, v in counts[2].items() if k in names}


class Keys:
    """Resolve a ``KeyRef`` to the key.

    A 1Password key is read once and kept in memory (``op`` can wait on a human); the others are
    read each time, so changing the file or the variable takes effect without a restart.
    """

    def __init__(self, read_op: Callable[[str], onepassword.Secret] = onepassword.read) -> None:
        self._read_op = read_op
        self._op: dict[str, str] = {}

    def get(self, ref: KeyRef) -> str:
        """The key for ``ref``.

        Raises:
            GatewayError: when it is empty, unset or unreadable. The message never holds it.
        """
        if ref.scheme == "env":
            value = os.environ.get(ref.value, "")
        elif ref.scheme == "file":
            try:
                value = Path(ref.value).expanduser().read_text(encoding="utf-8").strip()
            except OSError as exc:
                msg = f"cannot read key file {ref.value}: {exc.strerror}"
                raise GatewayError(msg) from exc
        else:
            if ref.value not in self._op:
                try:
                    self._op[ref.value] = self._read_op(ref.value).reveal()
                except onepassword.OpError as exc:
                    msg = f"cannot read the 1Password key: {exc}"
                    raise GatewayError(msg) from exc
            value = self._op[ref.value]
        if not value:
            msg = f"the key {ref.scheme}:... is empty"
            raise GatewayError(msg)
        return value

    def forget(self, ref: KeyRef) -> None:
        """Drop a kept key, so the next request reads it again (after the upstream refused it)."""
        self._op.pop(ref.value, None)


def inspect(path: str, body: bytes) -> Job:
    """What the request asks for: its model, and whether it uses a server tool.

    A server tool is a tool with a ``type`` other than ``custom`` (``web_search_20250305`` and the
    like): the provider runs it, so an upstream that does not have it must not get the request.
    """
    job = Job(path=path)
    if not body or not path.split("?", 1)[0].startswith("/v1/messages"):
        return job
    try:
        data = json.loads(body)
    except ValueError:
        return job
    if not isinstance(data, dict):
        return job
    tools = data.get("tools")
    server = any(
        isinstance(t, dict) and t.get("type") not in (None, "custom")
        for t in (tools if isinstance(tools, list) else ())
    )
    model = data.get("model")
    return replace(
        job,
        model=model if isinstance(model, str) else None,
        server_tool=server,
        stream=data.get("stream") is True,
        conversation=conversation_key(data),
    )


def conversation_key(data: Mapping[str, Any]) -> str | None:
    """A short hash of the first system block and the first user message, or None.

    The Messages API has no conversation id. Later turns repeat both, so they give the same key.
    """
    messages = data.get("messages")
    first = (
        next((m for m in messages if isinstance(m, Mapping) and m.get("role") == "user"), None)
        if isinstance(messages, list)
        else None
    )
    if first is None:
        return None
    system = data.get("system")
    head = system[0] if isinstance(system, list) and system else system
    raw = json.dumps([head, first.get("content")], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def skip_reason(up: Upstream, job: Job) -> str | None:
    """Why ``up`` cannot serve ``job`` at all (not a failure: it is not set aside), or None."""
    route = job.path.split("?", 1)[0]
    if up.kind == _INVOKE and route != "/v1/messages":
        return "path"  # Bedrock has no counterpart of count_tokens, files or models
    if up.kind != "passthrough" and not route.startswith("/v1/messages"):
        return "path"
    if up.kind == _INVOKE and job.model is None:
        return "model"
    if up.models and job.model is not None and job.model not in up.models:
        return "model"
    if job.server_tool and not up.server_tools:
        return "server tool"
    return None


def _drop(beta: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(beta, p) for p in patterns)


def request_headers(
    up: Upstream, client: Sequence[tuple[str, str]], keys: Keys
) -> list[tuple[str, str]]:
    """The headers to send to ``up``: the client's, minus hop-by-hop, with ``up``'s credential.

    A passthrough upstream gets the client's own ``authorization`` and ``x-api-key``. A key
    upstream gets neither of them: the client's credential is for another upstream. The values of
    every ``anthropic-beta`` header are joined, less what ``up`` drops, and sent as one.

    Raises:
        GatewayError: when a key upstream's key cannot be read.
    """
    out: list[tuple[str, str]] = []
    betas: list[str] = []
    for k, v in client:
        low = k.lower()
        if low in _NOT_FORWARDED or (up.kind != "passthrough" and low in _AUTH_HEADERS):
            continue
        if up.kind == _INVOKE and low == "anthropic-version":
            continue  # Bedrock takes the version in the body
        if low == "anthropic-beta":
            betas += [b.strip() for b in v.split(",") if b.strip()]
        else:
            out.append((k, v))
    kept = [b for b in betas if not _drop(b, up.drop_betas)]
    if kept:
        out.append(("anthropic-beta", ",".join(kept)))
    out.append(("Accept-Encoding", "identity"))
    if up.kind != "passthrough" and up.key:
        secret = keys.get(up.key)
        if up.auth in ("x-api-key", "both"):
            out.append(("x-api-key", secret))
        if up.auth in ("bearer", "both"):
            out.append(("Authorization", f"Bearer {secret}"))
    return out


def request_body(up: Upstream, job: Job, body: bytes) -> bytes:
    """``body`` with the ``model`` renamed for ``up`` when its ``models`` table maps it."""
    if not up.models or job.model is None or job.model not in up.models:
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    data["model"] = up.models[job.model]
    return json.dumps(data).encode()


@dataclass(frozen=True)
class Wire:
    """What is sent to an upstream for one request."""

    path: str
    body: bytes
    headers: list[tuple[str, str]]
    events: bool = False  # the answer is an AWS event stream to turn into server-sent events
    changes: tuple[str, ...] = ()  # body fields that were rewritten or dropped, for the log
    stripped: int = 0  # thinking blocks removed from the history


_INVOKE_REFUSES = ("context_management", "service_tier")  # Bedrock's Invoke API: 400 on these


def normalise(up: Upstream, data: dict[str, Any]) -> list[str]:
    """Change ``data`` in place to what ``up`` accepts. Returns what changed, for the log.

    Bedrock's Invoke API lags the first-party API: it refuses ``thinking.type = "enabled"``,
    ``context_management``, ``service_tier``, and ``top_p`` next to ``temperature``. ``drop_fields``
    of any non-passthrough upstream is dropped too.
    """
    changes: list[str] = []
    if up.kind == _INVOKE:
        thinking = data.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            data["thinking"] = {"type": "adaptive"}
            changes.append("thinking enabled -> adaptive")
        thinking = data.get("thinking")
        if (
            isinstance(thinking, dict)
            and thinking.get("type") in ("enabled", "adaptive")
            and data.get("temperature") not in (None, 1)
        ):
            del data["temperature"]  # only 1 is allowed while thinking is on (#61)
            changes.append("dropped temperature (thinking is on)")
        if "top_p" in data and "temperature" in data:
            del data["top_p"]
            changes.append("dropped top_p")
    for name in (*(_INVOKE_REFUSES if up.kind == _INVOKE else ()), *up.drop_fields):
        if name in data:
            del data[name]
            changes.append(f"dropped {name}")
    return changes


def strip_thinking(data: dict[str, Any]) -> int:
    """Remove the thinking blocks of assistant messages from ``data`` in place.

    A signature belongs to the upstream that issued it, so another upstream refuses it (#61).
    An assistant message left with no content is removed. Returns how many blocks were removed.
    """
    messages = data.get("messages")
    if not isinstance(messages, list):
        return 0
    removed = 0
    kept: list[Any] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and isinstance(content, list)
        ):
            blocks = [
                b for b in content if not (isinstance(b, dict) and b.get("type") in _THINKING)
            ]
            removed += len(content) - len(blocks)
            if not blocks:
                continue
            message = {**message, "content": blocks}
        kept.append(message)
    if removed:
        data["messages"] = kept
    return removed


def prepare(
    up: Upstream,
    job: Job,
    body: bytes,
    headers: list[tuple[str, str]],
    *,
    strip: bool = False,
) -> Wire:
    """The path, body and headers to send ``up`` for ``job``.

    A passthrough or key upstream gets the request as it is, less a renamed model and the fields
    of its ``drop_fields``. For Bedrock's Invoke API the path names the model, the body drops
    ``model`` and ``stream`` and gains ``anthropic_version``, the ``anthropic-beta`` values move
    into the body, and the fields Bedrock refuses are rewritten or dropped (``normalise``).

    With ``strip``, the thinking blocks of earlier assistant messages are removed (#61). Manual
    thinking (``type`` ``enabled``) needs the last assistant turn to start with a thinking block,
    so it is turned off for that request.
    """
    base = urlsplit(up.base_url).path
    invoke = up.kind == _INVOKE and job.model is not None and job.model in up.models
    plain = Wire(base + job.path, body if invoke else request_body(up, job, body), headers)
    if invoke:
        data = json.loads(body)
        data.pop("model", None)
        data.pop("stream", None)
    else:
        out = plain.body
        if not (up.drop_fields or (strip and _SIGNED in out)):
            return plain
        try:
            data = json.loads(out)
        except ValueError:
            return plain
        if not isinstance(data, dict):
            return plain
    changes = normalise(up, data)
    removed = strip_thinking(data) if strip else 0
    if removed:
        changes.append(f"dropped {removed} thinking block(s) another upstream may have issued")
        thinking = data.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            del data["thinking"]
            changes.append("thinking off for this request")
    if not invoke:
        return replace(
            plain,
            body=json.dumps(data).encode() if changes else plain.body,
            changes=tuple(changes),
            stripped=removed,
        )
    data["anthropic_version"] = _ANTHROPIC_VERSION
    betas = [v for k, v in headers if k.lower() == "anthropic-beta"]
    if betas:
        data["anthropic_beta"] = [b for v in betas for b in v.split(",")]
    kept = [(k, v) for k, v in headers if k.lower() != "anthropic-beta"]
    action = "invoke-with-response-stream" if job.stream else "invoke"
    model = quote(up.models[job.model or ""], safe="")
    return Wire(
        f"{base}/model/{model}/{action}",
        json.dumps(data).encode(),
        kept,
        job.stream,
        tuple(changes),
        removed,
    )


def error_text(body: bytes) -> str:
    """The message of an upstream's JSON error, on one short line fit for a log, or "".

    It reads ``error.message`` (Anthropic) or ``message`` (Bedrock) and nothing else, so a body is
    never logged whole. A long run of token-like characters is replaced, in case an upstream
    echoes a credential.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if message is None and isinstance(data, dict):
        message = data.get("message")
    if not isinstance(message, str):
        return ""
    text = _TOKENISH.sub("<redacted>", " ".join(message.split()))
    return text if len(text) <= _ERROR_TEXT else text[: _ERROR_TEXT - 1] + "…"


def read_body(headers: Mapping[str, str], rfile: Any) -> bytes:
    """The request body: ``Content-Length`` bytes, or a chunked body decoded.

    Raises:
        GatewayError: when it is larger than ``MAX_BODY`` or the chunking is malformed.
    """
    low = {k.lower(): v for k, v in headers.items()}
    if "chunked" in low.get("transfer-encoding", "").lower():
        out = bytearray()
        while True:
            line = rfile.readline(1024).split(b";", 1)[0].strip()
            try:
                size = int(line, 16)
            except ValueError as exc:
                msg = "malformed chunked body"
                raise GatewayError(msg) from exc
            if size == 0:
                while rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass  # the trailer, which nobody sends
                return bytes(out)
            out += rfile.read(size)
            rfile.readline(8)  # the CRLF after the chunk
            if len(out) > MAX_BODY:
                msg = "request body too large"
                raise GatewayError(msg)
    try:
        length = int(low.get("content-length") or 0)
    except ValueError as exc:
        msg = "bad Content-Length"
        raise GatewayError(msg) from exc
    if length > MAX_BODY:
        msg = "request body too large"
        raise GatewayError(msg)
    return rfile.read(length) if length else b""


def refuse(headers: Mapping[str, str], port: int, token: str | None) -> tuple[int, str] | None:
    """``(status, reason)`` when a request must not be served, else None.

    A request with an ``Origin`` header comes from a web page, which must not spend your keys. The
    ``Host`` must be this server's own name. With a ``token`` the client must send it.
    """
    low = {k.lower(): v for k, v in headers.items()}
    if "origin" in low:
        return 403, "browser requests are refused"
    if low.get("host", "") not in {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        "127.0.0.1",
        "localhost",
    }:
        return 403, "host not allowed"
    if token is not None and not hmac.compare_digest(low.get(TOKEN_HEADER, ""), token):
        return 401, f"send the {TOKEN_HEADER} header"
    return None


def parse_duration(text: str) -> float:
    """Seconds in ``90s``, ``30m``, ``2h`` or ``1d``.

    Raises:
        GatewayError: on anything else.
    """
    m = _DURATION.fullmatch(text.strip().lower())
    if not m:
        msg = "give a duration like 30m, 2h or 1d"
        raise GatewayError(msg)
    return int(m.group(1)) * _UNITS[m.group(2)]


def read_force(path: Path | None, now: float) -> Force | None:
    """The upstream chosen with ``cabildo gateway use``, or None when none or it ran out."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        force = Force(str(data["name"]), float(data["until"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return force if force.until > now else None


def write_force(path: Path, force: Force | None) -> None:
    """Save the user's choice (None: back to automatic), atomically."""
    if force is None:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"name": force.name, "until": force.until}), encoding="utf-8")
    os.replace(tmp, path)


Connect = Callable[[Upstream, float], http.client.HTTPConnection]


def connect(up: Upstream, timeout: float) -> http.client.HTTPConnection:
    """An HTTP(S) connection to the host of ``up``."""
    parts = urlsplit(up.base_url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    return cls(parts.hostname or "", parts.port, timeout=timeout)


@dataclass(frozen=True)
class Conversation:
    """What the gateway knows about one conversation: who served it last, and if it is mixed.

    ``tainted`` is true once thinking blocks had to be removed: the client keeps resending them,
    and nobody can tell which upstream issued which, so they are removed from every later request.
    """

    issuer: str
    tainted: bool = False


@dataclass
class Gateway:
    """The routing, key lookup and connection factory a server's handlers share."""

    settings: Settings
    router: Router = field(init=False)
    keys: Keys = field(default_factory=Keys)
    connect: Connect = connect
    timeout: float = 600.0
    use_file: Path | None = None
    log: Callable[[str], None] = lambda _m: None
    wall: Callable[[], float] = time.time
    _active: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _issued: OrderedDict[str, Conversation] = field(default_factory=OrderedDict, init=False)

    def __post_init__(self) -> None:
        self.router = Router(self.settings)

    def issued(self, key: str | None) -> Conversation | None:
        """Which upstream last served this conversation, or None when the gateway does not know."""
        if key is None:
            return None
        with self._lock:
            return self._issued.get(key)

    def remember(self, key: str | None, upstream: str, *, tainted: bool) -> None:
        """Note that ``upstream`` served the conversation, and whether its history is mixed."""
        if key is None:
            return
        with self._lock:
            self._issued[key] = Conversation(upstream, tainted)
            self._issued.move_to_end(key)
            while len(self._issued) > MAX_CONVERSATIONS:
                self._issued.popitem(last=False)

    def enter(self) -> None:
        """Count one open connection."""
        with self._lock:
            self._active += 1

    def leave(self) -> None:
        """Stop counting a connection that closed."""
        with self._lock:
            self._active -= 1

    @property
    def active(self) -> int:
        """How many connections are open now."""
        with self._lock:
            return self._active

    def forced(self) -> str | None:
        """The upstream the user chose with ``cabildo gateway use``, if it is still in force."""
        force = read_force(self.use_file, self.wall())
        known = {u.name for u in self.settings.upstreams}
        return force.name if force and force.name in known else None

    def reconfigure(self, settings: Settings) -> None:
        """Use new settings for the next request.

        Requests in flight finish as they are, and an upstream that is set aside stays set aside
        if the new settings still name it.
        """
        names = {u.name for u in settings.upstreams}
        kept = {k: v for k, v in self.router.snapshot().items() if k[0] in names}
        counts = self.router.counts()
        self.settings = settings
        self.router = Router(settings, aside=kept)
        self.router.restore(counts)


def make_handler(gw: Gateway) -> type[BaseHTTPRequestHandler]:
    """The request handler class bound to ``gw``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # the body ends when the connection closes: streams as is

        def log_message(self, format: str, *args: Any) -> None:
            return

        def handle(self) -> None:
            gw.enter()
            try:
                super().handle()
            finally:
                gw.leave()

        def _guard(self) -> bool:
            """Answer and return False when the request must not be served."""
            token = None
            if gw.settings.token is not None:
                try:
                    token = gw.keys.get(gw.settings.token)
                except GatewayError as exc:
                    self._error(500, str(exc))
                    return False
            verdict = refuse(
                dict(self.headers.items()), int(getattr(self.server, "server_port", 0)), token
            )
            if verdict is not None:
                self._error(*verdict)
                return False
            return True

        def do_GET(self) -> None:
            if not self._guard():
                return
            if self.path == "/_cabildo/status":
                forced = gw.forced()
                data = json.dumps(
                    {
                        "forced": forced,
                        "upstreams": [asdict(s) for s in gw.router.status(forced)],
                    }
                ).encode()
                self._send(200, [("Content-Type", "application/json")], data, None)
                return
            self._forward()

        def do_POST(self) -> None:
            if self._guard():
                self._forward()

        do_PUT = do_DELETE = do_PATCH = do_POST  # noqa: N815

        def _error(self, status: int, message: str) -> None:
            body = json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": message}}
            ).encode()
            self._send(status, [("Content-Type", "application/json")], body, None)

        def _forward(self) -> None:
            try:
                body = read_body(dict(self.headers.items()), self.rfile)
            except GatewayError as exc:
                self._error(413 if "large" in str(exc) else 400, str(exc))
                return
            client = list(self.headers.items())
            job = inspect(self.path, body)
            started = time.monotonic()
            last: tuple[int, list[tuple[str, str]], bytes, bool, bool] | None = None
            candidates = gw.router.order(job.model, gw.forced())
            attempts = [(u, False) for u in candidates if skip_reason(u, job) is None]
            skipped = len(candidates) - len(attempts)
            for i, (up, forced) in enumerate(attempts):
                more = i < len(attempts) - 1
                try:
                    headers = request_headers(up, client, gw.keys)
                except GatewayError as exc:
                    gw.log(f"gateway: {up.name}: {exc}")
                    gw.router.set_aside(up, Exhausted("no usable key", AUTH_COOLDOWN_S))
                    continue
                conn = gw.connect(up, gw.timeout)
                try:
                    conn.connect()
                except (OSError, http.client.HTTPException) as exc:
                    gw.log(f"gateway: {up.name}: cannot connect: {type(exc).__name__}")
                    gw.router.set_aside(up, Exhausted("unreachable", UNREACHABLE_COOLDOWN_S))
                    conn.close()
                    continue
                # From here the request may have reached the upstream: never send it twice.
                try:
                    state = gw.issued(job.conversation)
                    strip = forced or (
                        state is not None and (state.tainted or state.issuer != up.name)
                    )
                    wire = prepare(up, job, body, headers, strip=strip)
                    if wire.changes:
                        gw.log(f"gateway: {up.name}: {'; '.join(wire.changes)}")
                    conn.putrequest(self.command, wire.path, skip_accept_encoding=True)
                    for k, v in wire.headers:
                        conn.putheader(k, v)
                    if wire.body or self.command in ("POST", "PUT", "PATCH"):
                        conn.putheader("Content-Length", str(len(wire.body)))
                    conn.endheaders(wire.body)
                    resp = conn.getresponse()
                except (OSError, http.client.HTTPException) as exc:
                    gw.router.failed(up, type(exc).__name__)
                    gw.log(f"gateway: {up.name}: {type(exc).__name__} after the request was sent")
                    conn.close()
                    self._error(504, f"{up.name} did not answer")
                    return
                peek = b""
                wrap = up.kind == _INVOKE and resp.status >= 400
                if resp.status >= 400 or wrap:
                    peek = resp.read(_BODY_PEEK)
                    if (
                        resp.status == 400
                        and not strip
                        and _SIGNED in body
                        and _SIGNATURE_ERROR.search(peek)
                    ):
                        # The upstream answered and did not process the request, so sending it
                        # again without the thinking blocks cannot repeat anything.
                        gw.log(
                            f"gateway: {up.name}: refused a thinking signature, retrying without"
                        )
                        conn.close()
                        attempts.insert(i + 1, (up, True))
                        continue
                    verdict = classify(
                        resp.status,
                        dict(resp.getheaders()),
                        peek,
                        kind="passthrough" if up.kind == "passthrough" else "key",
                    )
                    if verdict is not None:
                        if resp.status == 401 and up.key:
                            gw.keys.forget(up.key)
                        gw.router.set_aside(up, verdict, job.model)
                        gw.log(
                            f"gateway: {up.name} set aside {verdict.cooldown_s:.0f}s: "
                            f"{verdict.reason}" + _because(peek)
                        )
                        last = (resp.status, resp.getheaders(), peek, len(peek) < _BODY_PEEK, wrap)
                        conn.close()
                        if more:
                            continue
                        break
                gw.router.served(up)
                if resp.status < 400:
                    gw.remember(
                        job.conversation,
                        up.name,
                        tainted=wire.stripped > 0 or (state is not None and state.tainted),
                    )
                if wrap:
                    self._send(*_wrapped(resp.status, resp.getheaders(), peek), None)
                else:
                    self._send(resp.status, resp.getheaders(), peek, resp, wire.events)
                conn.close()
                ms = round((time.monotonic() - started) * 1000)
                gw.log(f"{up.name} {job.model or '-'} {resp.status} {ms}ms" + _because(peek))
                return
            if last is not None:
                if last[4]:
                    self._send(*_wrapped(last[0], last[1], last[2]), None)
                else:
                    self._send(last[0], last[1] if last[3] else _no_length(last[1]), last[2], None)
                return
            why = "no upstream can serve this request" if skipped else "no upstream is usable"
            self._error(502, why)

        def _send(
            self,
            status: int,
            headers: list[tuple[str, str]],
            prefix: bytes,
            stream: http.client.HTTPResponse | None,
            events: bool = False,
        ) -> None:
            decoder = eventstream.Decoder() if events else None
            if decoder is not None:
                headers = [
                    *(h for h in headers if h[0].lower() not in ("content-length", "content-type")),
                    ("Content-Type", "text/event-stream"),
                    ("Cache-Control", "no-cache"),
                ]
            try:
                self.send_response(status)
                for k, v in headers:
                    if k.lower() not in _HOP_BY_HOP:
                        self.send_header(k, v)
                self.end_headers()
                if prefix:
                    self.wfile.write(prefix)
                while stream is not None:
                    chunk = stream.read1(_CHUNK)
                    if not chunk:
                        break
                    if decoder is not None:
                        try:
                            chunk = b"".join(eventstream.to_sse(f) for f in decoder.feed(chunk))
                        except eventstream.EventStreamError:
                            self.wfile.write(eventstream.error_event("api_error", "bad stream"))
                            break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # the client went away

    return Handler


def _because(peek: bytes) -> str:
    """``: <message>`` for a log line, or "" when the answer carries no readable error message."""
    text = error_text(peek)
    return f": {text}" if text else ""


def _wrapped(
    status: int, headers: list[tuple[str, str]], body: bytes
) -> tuple[int, list[tuple[str, str]], bytes]:
    """A Bedrock error answer as an Anthropic one: its body in the Anthropic error shape."""
    kept = [(k, v) for k, v in headers if k.lower() not in ("content-length", "content-type")]
    return (
        status,
        [*kept, ("Content-Type", "application/json")],
        eventstream.error_body(status, body),
    )


def _no_length(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``headers`` less ``Content-Length``, for a body that was only read in part."""
    return [(k, v) for k, v in headers if k.lower() != "content-length"]


class _Server(ThreadingHTTPServer):
    request_queue_size = BACKLOG
    daemon_threads = True


def make_server(
    gw: Gateway, port: int | None = None, *, inherited: socket.socket | None = None
) -> ThreadingHTTPServer:
    """A server for ``gw`` on 127.0.0.1.

    ``port`` 0 picks a free one. ``inherited`` is a listening socket handed over by the process
    this one replaced: the server uses it and binds nothing.
    """
    if inherited is None:
        return _Server(("127.0.0.1", gw.settings.port if port is None else port), make_handler(gw))
    server = _Server(("127.0.0.1", 0), make_handler(gw), bind_and_activate=False)
    server.socket.close()
    server.socket = inherited
    server.server_address = inherited.getsockname()
    server.server_port = server.server_address[1]
    return server


def use_file() -> Path:
    """Where ``cabildo gateway use`` writes the upstream to try first."""
    return store.home() / "gateway-use.json"


def _inherited(port: int) -> socket.socket | None:
    """The listening socket the previous process left in ``HANDOFF_ENV``, if it is for ``port``."""
    raw = os.environ.pop(HANDOFF_ENV, "")
    if not raw.isdigit():
        return None
    try:
        sock = socket.socket(fileno=int(raw))
    except OSError:
        return None
    if port not in (0, sock.getsockname()[1]):  # the port changed in the config: bind a new one
        sock.close()
        return None
    return sock


BASE_URL_ENV = "ANTHROPIC_BASE_URL"
HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"


def claude_settings_path() -> Path:
    """The user-level Claude Code settings file, which every session reads at start."""
    root = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(root).expanduser() if root else Path.home() / ".claude") / "settings.json"


def _is_local(url: object) -> bool:
    return isinstance(url, str) and urlsplit(url).hostname in {"127.0.0.1", "localhost"}


def point_claude_at(path: Path, port: int, token: str | None) -> list[str]:
    """Set ``env.ANTHROPIC_BASE_URL`` in a Claude Code settings file, and the token header.

    Returns notes for the user. Keeps every other entry and saves the old file as ``.json.bak``.

    Raises:
        GatewayError: when the file is not a JSON object, or the settings already name another
            base URL (which this would overwrite).
    """
    data = _read_settings(path)
    env = data.setdefault("env", {})
    url = f"http://127.0.0.1:{port}"
    current = env.get(BASE_URL_ENV)
    if current not in (None, url) and not _is_local(current):
        msg = f"{path} already sets {BASE_URL_ENV}={current}; remove it first"
        raise GatewayError(msg)
    env[BASE_URL_ENV] = url
    lines = [x for x in str(env.get(HEADERS_ENV, "")).splitlines() if x and not _ours(x)]
    if token is not None:
        lines.append(f"{TOKEN_HEADER}: {token}")
    if lines:
        env[HEADERS_ENV] = "\n".join(lines)
    else:
        env.pop(HEADERS_ENV, None)
    _write_settings(path, data)
    if "apiKeyHelper" in data:
        return ["apiKeyHelper is set: Claude Code sends that key, not your login"]
    return []


def unpoint_claude(path: Path) -> bool:
    """Remove the gateway entries from a Claude Code settings file. True when it changed."""
    if not path.exists():
        return False
    data = _read_settings(path)
    env = data.get("env")
    if not isinstance(env, dict) or not _is_local(env.get(BASE_URL_ENV)):
        return False
    del env[BASE_URL_ENV]
    lines = [x for x in str(env.get(HEADERS_ENV, "")).splitlines() if x and not _ours(x)]
    if lines:
        env[HEADERS_ENV] = "\n".join(lines)
    else:
        env.pop(HEADERS_ENV, None)
    _write_settings(path, data)
    return True


def _ours(line: str) -> bool:
    return line.split(":", 1)[0].strip().lower() == TOKEN_HEADER


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        msg = f"cannot read {path}: {exc}"
        raise GatewayError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path} is not a JSON object"
        raise GatewayError(msg)
    return data


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(".json.bak").write_bytes(path.read_bytes())
    path.write_text(json.dumps(data, indent=2) + "\n")


class GatewayService:
    """A gateway server running on a thread of the daemon, like the LAN and WAN services."""

    def __init__(
        self, gw: Gateway, server: ThreadingHTTPServer, log: Callable[[str], None]
    ) -> None:
        self.gw = gw
        self.server = server
        self.log = log
        self.paused = False
        self.thread = threading.Thread(target=server.serve_forever, daemon=True, name="gateway")

    @property
    def port(self) -> int:
        """The port the server listens on."""
        return int(self.server.server_address[1])

    @property
    def active(self) -> int:
        """How many connections are open now."""
        return self.gw.active

    def start(self) -> None:
        """Start serving."""
        self.thread.start()

    def pause(self) -> None:
        """Stop accepting. The socket stays open, so new connections wait in its backlog."""
        if not self.paused:
            self.paused = True
            self.server.shutdown()

    def handoff(self) -> None:
        """Leave the listening socket open for the process that replaces this one.

        It passes a duplicate of the descriptor: the socket object closes its own descriptor when
        it is garbage collected, which happens when the daemon's ``main`` returns, before the exec.
        """
        fd = os.dup(self.server.socket.fileno())
        os.set_inheritable(fd, True)
        os.environ[HANDOFF_ENV] = str(fd)

    def close(self) -> None:
        """Stop serving and close the socket."""
        self.pause()
        self.server.server_close()


def start_service(log: Callable[[str], None]) -> GatewayService | None:
    """Start the gateway in this process, or return None when ``[gateway]`` is not configured.

    A bad config or a busy port is logged and the daemon goes on without the gateway.
    """
    table = config.gateway_table()
    if not table:
        return None
    try:
        settings = parse(table)
    except GatewayError as exc:
        log(f"gateway: not started: {exc}")
        return None
    gw = Gateway(settings, use_file=use_file(), log=log)
    try:
        server = make_server(gw, inherited=_inherited(settings.port))
    except OSError as exc:
        log(f"gateway: cannot listen on 127.0.0.1:{settings.port}: {exc}")
        return None
    svc = GatewayService(gw, server, log)
    svc.start()
    names = ", ".join(u.name for u in settings.upstreams)
    log(f"gateway on http://127.0.0.1:{svc.port} (in order: {names})")
    return svc
