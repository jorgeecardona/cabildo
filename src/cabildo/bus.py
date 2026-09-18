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
