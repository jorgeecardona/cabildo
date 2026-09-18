"""wan.py: sync a cabildo bus with explicit remote peers over a tunnel.

lan.py finds peers by UDP broadcast on the local subnet. wan.py is the same
pull-and-dedup machinery pointed at explicit peer addresses from config, for
peers reached over a tunnel -- a WireGuard/Tailscale mesh IP, or an SSH
port-forward landing on loopback -- rather than the local wire. It reuses lan.py
wholesale: ``pull_from_peer`` (client), ``run_server`` + ``handle_pull``
(server), ``select_outbound`` and ``merge_incoming`` (the sync core). The only
differences are:

  * explicit addresses instead of a beacon (broadcast does not cross subnets),
  * a shared bus token on the pull handshake (lan's ``secret`` slot), which is
    authorization on top of the already-encrypted tunnel,
  * rows stamped ``origin='wan'`` so they are dedup-merged and, like ``'lan'``,
    kept off the cloud uplink,
  * a per-peer cursor keyed by address but verified against the peer's node id, so
    a peer that restarts its DB resets the cursor instead of stalling above the
    peer's new ids, in ``wan_sync.json``.

The transport stays stdlib (raw TCP + newline JSON, via lan). cabildo binds only
to the address it is told to -- loopback for an SSH forward, the mesh IP for
WireGuard -- never implicitly to the public internet. Wiring this into the
daemon and reading peers from ``[remote]`` config is a later step; this module is
the mechanism.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cloud, lan, store

ORIGIN = "wan"


@dataclass(frozen=True)
class Peer:
    """A remote bus reachable at a fixed address over the tunnel."""

    host: str
    port: int

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"


def parse_peer(spec: str, *, default_port: int = lan.BEACON_PORT) -> Peer:
    """``"host:port"`` or bare ``"host"`` (falling back to ``default_port``)."""
    host, sep, port = spec.rpartition(":")
    if not sep:
        return Peer(spec, default_port)
    return Peer(host, int(port))


# --------------------------------------------------------------------------- #
# Per-peer cursor: keyed by address, but each entry also stores the peer's node id
# so a peer restart is detected and the cursor reset (lan keys by node id directly).
# --------------------------------------------------------------------------- #
def _cursor_file() -> Path:
    return store.home() / "wan_sync.json"


def _load_cursor() -> dict[str, Any]:
    try:
        return json.loads(_cursor_file().read_text())
    except (OSError, ValueError):
        return {"peers": {}}


def _save_cursor(cur: dict[str, Any]) -> None:
    try:
        _cursor_file().write_text(json.dumps(cur))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Server: bind an explicit address, serve pulls with lan's handler
# --------------------------------------------------------------------------- #
def open_server(host: str, port: int = 0) -> socket.socket:
    """Bind a TCP listener on an explicit ``host`` (the tunnel or loopback
    interface, never ``0.0.0.0`` by default). ``port=0`` picks a free port.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(16)
    return s


def serve(
    sock: socket.socket,
    project: str,
    token: str | None,
    stop: threading.Event,
    *,
    conn_factory: Any = None,
) -> None:
    """Serve pulls until ``stop`` is set, gating on ``token`` (lan's secret)."""
    lan.run_server(sock, project, conn_factory or store.connect, token, stop)


# --------------------------------------------------------------------------- #
# Client: pull each configured peer and merge as origin='wan'
# --------------------------------------------------------------------------- #
def sync_round(
    peers: list[Peer],
    project: str,
    token: str | None,
    *,
    conn: Any = None,
    node: str | None = None,
) -> dict[str, int]:
    """Pull each peer's project rows newer than our cursor, merge them stamped
    ``origin='wan'``, and advance each peer's cursor. Best-effort per peer: a
    peer that is down leaves its cursor untouched and is retried next round.
    """
    node = node or lan.node_id()
    own = conn is None
    conn = conn or store.connect()
    cur = _load_cursor()
    pc: dict[str, Any] = cur.setdefault("peers", {})
    pulled = merged = 0
    try:
        for p in peers:
            raw = pc.get(p.key)
            entry = raw if isinstance(raw, dict) else {"cursor": int(raw or 0), "node": None}
            since = int(entry.get("cursor", 0))
            rows, max_id, peer_node = lan.pull_from_peer(
                p.host, p.port, project, since, token, node
            )
            # If the peer restarted its DB it comes back with a new node id, so our
            # cursor now sits above the peer's restarted ids and would stall forever
            # (finding #2). Detect the generation change and re-pull from the start.
            if peer_node is not None and entry.get("node") not in (None, peer_node):
                rows, max_id, peer_node = lan.pull_from_peer(
                    p.host, p.port, project, 0, token, node
                )
            if rows:
                merged += lan.merge_incoming(conn, rows, origin=ORIGIN)
            pc[p.key] = {"cursor": max_id, "node": peer_node or entry.get("node")}
            pulled += len(rows)
    finally:
        if own:
            conn.close()
    _save_cursor(cur)
    return {"peers": len(peers), "pulled": pulled, "merged": merged}


# --------------------------------------------------------------------------- #
# Orchestration: serve + pull, one round or a loop (the `cabildo wan` command)
# --------------------------------------------------------------------------- #
def run(
    project: str,
    *,
    peers: list[str],
    token: str | None = None,
    bind: str | None = None,
    interval: float = 30.0,
    loop: bool = False,
) -> int:
    """Serve this bus to explicit peers and pull from them over the tunnel.

    ``peers`` are ``"host:port"`` strings; ``bind`` (``"host"`` or ``"host:port"``)
    is the address to serve pulls on, or ``None`` for pull-only. One round by
    default; ``loop`` keeps serving and syncing until interrupted.
    """
    peer_list = [parse_peer(p) for p in peers]
    key = cloud.project_key(project)
    stop = threading.Event()
    srv: socket.socket | None = None
    if bind:
        pb = parse_peer(bind)
        srv = open_server(pb.host, pb.port)
        host, port = srv.getsockname()[:2]
        threading.Thread(target=serve, args=(srv, project, token, stop), daemon=True).start()
        print(f"cabildo wan: serving '{key}' on {host}:{port}" + (" (token set)" if token else ""))
    else:
        print(f"cabildo wan: pull-only for '{key}' from {len(peer_list)} peer(s)")
    try:
        while True:
            r = sync_round(peer_list, project, token)
            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp} {r['peers']} peer(s) · pulled {r['pulled']} · merged {r['merged']} new")
            if not loop:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        stop.set()
        if srv is not None:
            srv.close()
    return 0
