"""wan.py: sync a cabildo bus with explicit remote peers over a tunnel.

pair.py pairs machines on the local subnet. wan.py points the plain pull-and-dedup
machinery in lan.py at explicit peer addresses from config, for
peers reached over a tunnel -- a WireGuard/Tailscale mesh IP, or an SSH
port-forward pushing on loopback -- rather than the local wire. It reuses lan.py
wholesale: ``pull_from_peer`` (client), ``run_server`` + ``handle_pull``
(server), ``select_outbound`` and ``merge_incoming`` (the sync core). The only
differences are:

  * explicit addresses (broadcast does not cross subnets),
  * a shared bus token on the pull handshake (lan's ``secret`` slot), which is
    authorization on top of the already-encrypted tunnel,
  * rows stamped ``provenance='wan'`` so they are dedup-merged and, like ``'lan'``,
    kept off the cloud uplink,
  * a per-peer cursor keyed by address but verified against the peer's node id, so
    a peer that restarts its DB resets the cursor instead of stalling above the
    peer's new ids, in ``wan_sync.json``.

The transport stays stdlib (raw TCP + newline JSON, via lan). cabildo binds only
to the address it is told to -- loopback for an SSH forward, the mesh IP for
WireGuard -- never implicitly to the public internet.

The daemon runs this (``Service``) from the machine-wide config, one table per project:

    [project.cabildo.remote]
    peers = ["100.64.0.2:48653"]
    token = "..."
    bind = "100.64.0.1:48653"
    interval = 30
"""

from __future__ import annotations

import dataclasses
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, lan, store

# Addresses that expose the server beyond the intended tunnel/loopback interface.
_WILDCARD_HOSTS = {"0.0.0.0", "::", "*", ""}  # noqa: S104

ORIGIN = "wan"


# The port a peer spec without ``:port`` points at.
DEFAULT_PORT = 48653


@dataclass(frozen=True)
class Peer:
    """A remote bus reachable at a fixed address over the tunnel."""

    host: str
    port: int

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Link:
    """One project's WAN sync: who to pull from, and where to serve pulls."""

    project: str
    peers: tuple[Peer, ...]
    token: str | None
    bind: Peer | None
    interval: float


def parse_peer(spec: str, *, default_port: int = DEFAULT_PORT) -> Peer:
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


def _status_file() -> Path:
    return store.home() / "wan_status.json"


def load_status() -> dict[str, lan.SyncStatus]:
    """The last pull per ``project@host:port``. Empty when none is recorded."""
    try:
        raw = json.loads(_status_file().read_text())
        return {key: lan.SyncStatus(**v) for key, v in raw.items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_status(status: dict[str, lan.SyncStatus]) -> None:
    try:
        _status_file().write_text(json.dumps({k: dataclasses.asdict(v) for k, v in status.items()}))
    except OSError:
        pass


def status_key(project: str, peer: Peer) -> str:
    """The cursor and status key: the same address can serve two projects."""
    return f"{project}@{peer.key}"


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
# Client: pull each configured peer and merge as provenance='wan'
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
    ``provenance='wan'``, and advance each peer's cursor. Best-effort per peer: a
    peer that is down leaves its cursor untouched and is retried next round.
    """
    node = node or lan.node_id()
    own = conn is None
    conn = conn or store.connect()
    cur = _load_cursor()
    pc: dict[str, Any] = cur.setdefault("peers", {})
    status = load_status()
    pulled = merged = 0
    try:
        for p in peers:
            key = status_key(project, p)
            raw = pc.get(key)
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
            new = lan.merge_incoming(conn, rows, provenance=ORIGIN) if rows else 0
            merged += new
            pc[key] = {"cursor": max_id, "node": peer_node or entry.get("node")}
            pulled += len(rows)
            error = "" if peer_node else "unreachable or refused"
            status[key] = lan.SyncStatus(time.time(), len(rows), new, max_id, error)
    finally:
        if own:
            conn.close()
    _save_cursor(cur)
    _save_status(status)
    return {"peers": len(peers), "pulled": pulled, "merged": merged}


# --------------------------------------------------------------------------- #
# The daemon's WAN service
# --------------------------------------------------------------------------- #
def _insecure_bind_reason(host: str, token: str | None) -> str | None:
    """Why serving on ``host`` with ``token`` would be unauthenticated, or None."""
    if host in _WILDCARD_HOSTS:
        return f"refusing to bind the wildcard address {host!r} (reachable off the tunnel)"
    if not token:
        return "refusing to serve without a token (an unauthenticated bus)"
    return None


def load_links() -> list[Link]:
    """Every ``[project.<name>.remote]`` table in the machine-wide config."""
    links: list[Link] = []
    for project, raw in config.remote_tables().items():
        peers = raw.get("peers", [])
        peers = [peers] if isinstance(peers, str) else peers
        bind = raw.get("bind")
        try:
            links.append(
                Link(
                    project=project,
                    peers=tuple(parse_peer(str(p)) for p in peers),
                    token=str(raw["token"]) if raw.get("token") else None,
                    bind=parse_peer(str(bind)) if bind else None,
                    interval=float(raw.get("interval", 30.0)),
                )
            )
        except (TypeError, ValueError):
            continue  # one bad table must not stop the others
    return links


class Service:
    """Serve and pull every configured link, inside the daemon."""

    def __init__(self, links: list[Link], log: Callable[[str], None]) -> None:
        self.links = links
        self.log = log
        self.stop = threading.Event()
        self.servers: list[socket.socket] = []
        self.due = dict.fromkeys((link.project for link in links), 0.0)

    def start(self) -> None:
        """Open the listeners and start the pull thread."""
        for link in self.links:
            if link.bind:
                self._serve(link, link.bind)
        threading.Thread(target=self._loop, daemon=True).start()

    def _serve(self, link: Link, bind: Peer) -> None:
        refusal = _insecure_bind_reason(bind.host, link.token)
        if refusal:
            self.log(f"wan: {link.project}: {refusal}")
            return
        try:
            srv = open_server(bind.host, bind.port)
        except OSError as e:
            self.log(f"wan: {link.project}: cannot bind {bind.key}: {e}")
            return
        self.servers.append(srv)
        threading.Thread(
            target=serve, args=(srv, link.project, link.token, self.stop), daemon=True
        ).start()
        self.log(f"wan: {link.project}: serving on {bind.key}")

    def sync_due(self, now: float) -> int:
        """Pull the links whose interval has passed. Returns messages merged."""
        merged = 0
        for link in self.links:
            if not link.peers or now < self.due[link.project]:
                continue
            self.due[link.project] = now + link.interval
            r = sync_round(list(link.peers), link.project, link.token)
            merged += r["merged"]
            if r["merged"]:
                self.log(f"wan: {link.project}: merged {r['merged']} of {r['pulled']}")
        return merged

    def _loop(self) -> None:
        while not self.stop.wait(1.0):
            try:
                self.sync_due(time.time())
            except Exception as e:  # one bad round must not stop the loop
                self.log(f"wan: sync: {type(e).__name__}: {e}")

    def close(self) -> None:
        self.stop.set()
        for srv in self.servers:
            srv.close()


def start_service(log: Callable[[str], None]) -> Service | None:
    """Start the WAN service, or return None when no link is configured."""
    links = load_links()
    if not links:
        return None
    svc = Service(links, log)
    svc.start()
    return svc


def peers_report() -> list[dict[str, Any]]:
    """Each configured WAN peer and its last pull. Tokens are left out."""
    status = load_status()
    out: list[dict[str, Any]] = []
    for link in load_links():
        for peer in link.peers:
            st = status.get(status_key(link.project, peer))
            out.append(
                {
                    "transport": "wan",
                    "project": link.project,
                    "address": peer.key,
                    "last_pull": dataclasses.asdict(st) if st else None,
                }
            )
    return out
