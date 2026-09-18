"""bus.py: the seam between a cabildo participant and the data store.

Today every process opens the SQLite file directly (``store.connect()``). This
module introduces one interface -- ``Bus`` -- that a call site talks to instead,
so a single place decides whether the work runs against a local SQLite connection
or is sent to the broker service over IPC.

Phase 1 (this file, so far): the ``Bus`` protocol, the operation registry, and
``LocalBus`` -- the direct-SQLite backend, behaviour identical to today. Nothing
in the package routes through it yet; call sites migrate once the surface is
proven. The socket backend and the broker server come in later phases (see
``docs/design/broker-service.md``).

Why an operation registry and not one RPC per store function: an operation can
span several store calls under one transaction (``join_topic`` is
``ensure_topic`` + ``subscribe`` + one commit), and identity is caller-local
(``mcp_server._me`` reads the caller's env and PID). So a Bus call is one
high-level operation carrying an explicit identity envelope, and the whole
operation runs on the owning side against one connection.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import store


@dataclass(frozen=True)
class Identity:
    """Who is making the call, resolved by the client before the call crosses the
    seam. The broker cannot see the caller's process, so identity travels with
    every request rather than being derived on the owning side.
    """

    session: str
    handle: str | None = None
    project: str | None = None
    project_name: str | None = None


# A handler runs one operation against an open connection, with the caller's
# identity and the request params, and returns a JSON-serialisable result -- so
# the same handler works whether it runs locally or is dispatched from a socket
# request in a later phase.
Handler = Callable[[sqlite3.Connection, Identity, Mapping[str, Any]], Any]

OPS: dict[str, Handler] = {}


def op(name: str) -> Callable[[Handler], Handler]:
    """Register a handler under an operation name (the wire verb)."""

    def register(fn: Handler) -> Handler:
        if name in OPS:
            raise ValueError(f"duplicate bus op: {name}")
        OPS[name] = fn
        return fn

    return register


class Bus(Protocol):
    """One operation in, one JSON-serialisable result out. The only method a call
    site needs, whichever backend is behind it.
    """

    def call(self, op: str, params: Mapping[str, Any], *, identity: Identity) -> Any: ...


class LocalBus:
    """Bus backed by a direct ``store.connect()``: today's behaviour, one
    connection per call. The default when no broker service is running.
    """

    def call(self, op: str, params: Mapping[str, Any], *, identity: Identity) -> Any:
        handler = OPS.get(op)
        if handler is None:
            raise KeyError(f"unknown bus op: {op}")
        with store.connect() as conn:
            return handler(conn, identity, params)


# --------------------------------------------------------------------------- #
# Wire protocol -- newline-delimited JSON, one request/response per line
# --------------------------------------------------------------------------- #
# A single line is capped so a malformed or hostile peer cannot make the reader
# buffer without bound.
MAX_LINE = 8 * 1024 * 1024


class BusError(RuntimeError):
    """The broker refused or failed a call (unknown op, or the handler raised).

    Carries the server-reported error type so the caller can tell a bad request
    from a handler failure without parsing the message.
    """

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


def identity_to_wire(ident: Identity) -> dict[str, Any]:
    return {
        "session": ident.session,
        "handle": ident.handle,
        "project": ident.project,
        "project_name": ident.project_name,
    }


def identity_from_wire(d: Mapping[str, Any]) -> Identity:
    return Identity(
        session=str(d["session"]),
        handle=d.get("handle"),
        project=d.get("project"),
        project_name=d.get("project_name"),
    )


def encode_request(op: str, params: Mapping[str, Any], identity: Identity) -> bytes:
    """One request line (no trailing newline; the sender appends it)."""
    return json.dumps(
        {"op": op, "params": dict(params), "identity": identity_to_wire(identity)}
    ).encode()


class LineReader:
    """Read newline-delimited frames off a stream socket, buffering across recv()
    boundaries and never letting one frame exceed ``MAX_LINE``.
    """

    def __init__(self, sock: socket.socket, cap: int = MAX_LINE) -> None:
        self._sock = sock
        self._cap = cap
        self._buf = bytearray()

    def readline(self) -> bytes | None:
        """The next frame without its newline, or None at end of stream."""
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                return line
            chunk = self._sock.recv(65536)
            if not chunk:
                return bytes(self._buf) if self._buf else None
            self._buf.extend(chunk)
            if len(self._buf) > self._cap:
                raise BusError("frame exceeds size cap", kind="too_large")


class SocketBus:
    """Bus backed by a socket to the broker service. One request per connection:
    connect, send the framed request, read one response frame, close.
    """

    def __init__(self, address: str, *, timeout: float = 5.0) -> None:
        self.address = address
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.address)
        return s

    def call(self, op: str, params: Mapping[str, Any], *, identity: Identity) -> Any:
        with self._connect() as s:
            s.sendall(encode_request(op, params, identity) + b"\n")
            line = LineReader(s).readline()
        if line is None:
            raise BusError("no response from broker", kind="no_response")
        resp = json.loads(line)
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise BusError(
                str(err.get("message") or "broker error"), kind=str(err.get("type") or "error")
            )
        return resp.get("result")


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
_BROKER_OFF = ("0", "off", "no", "false")


def default_socket_path() -> str:
    """The broker's unix socket, beside the bus DB, unless overridden by
    ``CABILDO_BROKER_SOCKET``.
    """
    return os.environ.get("CABILDO_BROKER_SOCKET") or str(store.home() / "bus.sock")


def _socket_live(path: str) -> bool:
    """Is a broker actually listening on ``path``? A stale socket file left by a
    crashed daemon exists but refuses connections, so probe rather than stat.
    """
    if not os.path.exists(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        probe.connect(path)
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def default_bus() -> Bus:
    """``SocketBus`` when a broker is live, else ``LocalBus``.

    Opt-out and self-healing: with no broker running (the zero-service local
    default), or ``CABILDO_BROKER`` set to a falsey value, a client just opens the
    DB directly as it always has. When a broker is up, the client routes through
    it -- the same selection a remote agent uses to reach its own local broker.
    """
    if os.environ.get("CABILDO_BROKER", "").strip().lower() in _BROKER_OFF:
        return LocalBus()
    path = default_socket_path()
    return SocketBus(path) if _socket_live(path) else LocalBus()


# --------------------------------------------------------------------------- #
# Operations -- reads
# --------------------------------------------------------------------------- #
@op("list_topics")
def _list_topics(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    return store.list_topics(conn)


@op("list_agents")
def _list_agents(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    rows = store.active_sessions(conn, project=params.get("project"), exclude=params.get("exclude"))
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["presence"] = store.presence_state(r)
        d["display"] = store.display_name(r, r["session_id"])
        out.append(d)
    return out


@op("inbox")
def _inbox(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    return store.inbox(
        conn,
        ident.session,
        unread_only=bool(params.get("unread_only", True)),
        limit=int(params.get("limit", 50)),
    )


@op("history")
def _history(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    raw = params.get("between")
    between: tuple[str, str] | None = (str(raw[0]), str(raw[1])) if raw else None
    return store.history(
        conn,
        topic=params.get("topic"),
        between=between,
        limit=int(params.get("limit", 50)),
    )


@op("get_message")
def _get_message(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    return store.get_message(conn, ident.session, int(params["message_id"]))


@op("list_facts")
def _list_facts(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    return store.list_facts(
        conn,
        project_name=params.get("project_name") or ident.project_name,
        kind=params.get("kind"),
        query=params.get("query"),
        limit=int(params.get("limit", 20)),
    )


@op("claims")
def _claims(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    return store.list_claims(conn, project=params.get("project") or ident.project)


# --------------------------------------------------------------------------- #
# Operations -- writes
# --------------------------------------------------------------------------- #
@op("send")
def _send(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    mid = store.post_message(
        conn,
        from_session=ident.session,
        from_handle=ident.handle,
        body=str(params["body"]),
        topic=params.get("topic"),
        to_session=params.get("to_session"),
        project=params.get("project") or ident.project,
        urgent=bool(params.get("urgent", False)),
    )
    return {"id": mid}


@op("mark_read")
def _mark_read(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    ids = params.get("message_ids")
    n = store.mark_read(
        conn,
        ident.session,
        message_ids=list(ids) if ids is not None else None,
        topic=params.get("topic"),
        all_unread=bool(params.get("all_unread", False)),
    )
    return {"marked": n}


@op("set_handle")
def _set_handle(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    handle = str(params["handle"])
    store.set_handle(conn, ident.session, handle)
    return {"ok": True, "handle": handle}


@op("claim")
def _claim(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    return store.acquire_claim(
        conn,
        resource=str(params["resource"]),
        holder_session=ident.session,
        holder_handle=ident.handle,
        project=ident.project,
        ttl_seconds=float(params.get("ttl_seconds", 600)),
        note=params.get("note"),
    )


@op("release")
def _release(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    resource = str(params["resource"])
    ok = store.release_claim(conn, resource=resource, holder_session=ident.session)
    return {"ok": ok, "resource": resource}


# Transactional ops: several store calls under one commit, run whole on the owning
# side. join_topic is ensure_topic + subscribe + commit (see mcp_server.join_topic).
@op("join_topic")
def _join_topic(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    topic = str(params["topic"])
    store.ensure_topic(conn, topic, created_by=ident.session)
    store.subscribe(conn, ident.session, topic)
    conn.commit()
    return {"ok": True, "topic": topic}


@op("leave_topic")
def _leave_topic(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    topic = str(params["topic"])
    store.unsubscribe(conn, ident.session, topic)
    conn.commit()
    return {"ok": True, "topic": topic}


@op("request")
def _request(conn: sqlite3.Connection, ident: Identity, params: Mapping[str, Any]) -> Any:
    from . import actions  # local: keeps bus import-light and avoids a cycle

    action = str(params.get("action", ""))
    if action not in actions.ACTIONS:
        return {
            "ok": False,
            "error": f"unknown action {action!r}; allowed: {sorted(actions.ACTIONS)}",
        }
    # Strip caller-supplied identity keys, then stamp from the resolved identity:
    # an agent must not be able to request in someone else's name.
    raw = params.get("params") or {}
    p = {k: v for k, v in raw.items() if not k.startswith("_")}
    p |= {"_from_session": ident.session, "_from_handle": ident.handle, "_project": ident.project}
    iid = store.request_intent(
        conn,
        from_session=ident.session,
        from_handle=ident.handle,
        project=ident.project,
        action=action,
        params=p,
    )
    return {"ok": True, "intent_id": iid, "status": "pending"}
