"""broker.py: the service-side socket server behind the Bus seam.

The daemon is the sole owner of the SQLite DB. This module lets it serve that DB
over a unix socket so MCP processes and hooks can be thin clients (see
``bus.SocketBus``) instead of opening the file themselves.

One connection, serialised. The broker opens exactly one ``store.connect()`` (with
``check_same_thread=False``) and guards it with a lock, so every request runs one
at a time against one connection -- the same serial-writer invariant the intents
queue already relies on. Accept runs on its own thread and each client is handled
on a short-lived thread; the lock is what keeps them from tripping over the shared
connection.

Phase 2: this server is standalone and tested here. Wiring it into ``daemon.py``'s
poll loop (so a running ``cabildo service`` is also the broker) is the next step;
see ``docs/design/broker-service.md``.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import threading

from . import bus, store


def _error(kind: str, message: str) -> bytes:
    return json.dumps({"ok": False, "error": {"type": kind, "message": message}}).encode()


def dispatch(conn: sqlite3.Connection, lock: threading.Lock, line: bytes) -> bytes:
    """Run one request frame against the owned connection and return the response
    frame (no trailing newline). Never raises: a bad request or a failing handler
    comes back as an error response, because one client must not take the broker
    down for the others.
    """
    try:
        req = json.loads(line)
        op = str(req["op"])
        params = req.get("params") or {}
        identity = bus.identity_from_wire(req["identity"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return _error("bad_request", f"malformed request: {e}")

    handler = bus.OPS.get(op)
    if handler is None:
        return _error("unknown_op", f"unknown bus op: {op}")
    try:
        with lock:
            try:
                result = handler(conn, identity, params)
            except Exception:
                # A handler that raised mid-write (e.g. a bad bind after the
                # implicit BEGIN) leaves the shared connection in an open
                # transaction. Roll it back here, under the lock, or the next op
                # pins a stale snapshot and every later write fails with "database
                # is locked" -- the broker wedged until restart (review finding #1).
                if conn.in_transaction:
                    conn.rollback()
                raise
    except Exception as e:  # a handler failure is data, not a broker crash
        return _error(type(e).__name__, str(e))
    return json.dumps({"ok": True, "result": result}).encode()


def _serve_client(client: socket.socket, conn: sqlite3.Connection, lock: threading.Lock) -> None:
    with contextlib.closing(client):
        reader = bus.LineReader(client)
        while True:
            try:
                line = reader.readline()
            except (OSError, bus.BusError):
                return
            if not line:
                return
            with contextlib.suppress(OSError):
                client.sendall(dispatch(conn, lock, line) + b"\n")


def serve(
    address: str,
    *,
    ready: threading.Event | None = None,
    stop: threading.Event | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Serve the bus on a unix socket at ``address`` until ``stop`` is set.

    ``conn`` lets the daemon pass its own owned connection; left None, the broker
    opens one (cross-thread, guarded by its lock). ``ready`` is set once the socket
    is listening, so a caller can wait for it without a sleep.
    """
    stop = stop or threading.Event()
    owns_conn = conn is None
    conn = conn if conn is not None else store.connect(check_same_thread=False)
    lock = threading.Lock()

    with contextlib.suppress(FileNotFoundError):
        os.unlink(address)  # clear a stale socket from a crashed run
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(address)
        os.chmod(address, 0o600)  # user-only: the local socket's whole access control
        srv.listen(64)
        srv.settimeout(0.5)  # wake periodically to check `stop`
        if ready is not None:
            ready.set()
        while not stop.is_set():
            try:
                client, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=_serve_client, args=(client, conn, lock), daemon=True).start()
    finally:
        srv.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(address)
        if owns_conn:
            conn.close()


def socket_path() -> str:
    """The default broker socket (shared with the client's selection logic)."""
    return bus.default_socket_path()
