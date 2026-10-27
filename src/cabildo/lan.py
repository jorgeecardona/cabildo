"""Message sync between machines: the daemon's LAN service and the pull pieces
``wan.py`` shares.

The daemon runs ``Service`` (see ``start_service``): a TLS server and a pull loop
over the machines paired with ``request("pair")`` (pairing and TLS live in
``pair.py``). Each side pulls the other's messages; nothing writes to a peer's
bus directly.

Messages carry a global ``guid`` (see ``store.post_message``). A pulled row is
inserted only if its guid is new, stamped with its provenance, keeping the
original guid and ``created_at`` so a message that hops A->B->C merges once.

``run_server``, ``pull_from_peer`` and ``select_outbound`` are the older plain
TCP pull used by ``wan.py`` over a tunnel. Stdlib only.
"""

from __future__ import annotations

import json
import socket
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import cloud, store

# Wire version. Bumped only on an incompatible change to the beacon or the pull
# request/response shape; peers ignore beacons and refuse pulls at other protos.
PROTO = 1
# The well-known UDP port peers broadcast their presence on. TCP is ephemeral
# (advertised in the beacon), so only this one port is fixed.
BEACON_PORT = 48653
PULL_LIMIT = 300
_CONN_TIMEOUT = 5.0
_RECV_CAP = 16 * 1024 * 1024  # a pull response is bounded by PULL_LIMIT rows


# --------------------------------------------------------------------------- #
# Node identity — a stable per-machine id so a peer can tell "me" from a real
# peer's beacon and key its per-peer sync cursor. Lives beside the bus.
# --------------------------------------------------------------------------- #
def _node_file() -> Path:
    return store.home() / "lan.json"


def node_id() -> str:
    p = _node_file()
    try:
        nid = json.loads(p.read_text()).get("node")
        if nid:
            return str(nid)
    except Exception:
        pass
    nid = uuid.uuid4().hex
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"node": nid}))
    except Exception:
        pass
    return nid


# --------------------------------------------------------------------------- #
# Pure sync logic (no sockets) — the testable core.
# --------------------------------------------------------------------------- #
_MSG_COLS = (
    "id, guid, created_at, from_session, from_handle, topic, to_session, project, body, urgent"
)


def select_outbound(
    conn, project: str, since: int, limit: int = PULL_LIMIT
) -> tuple[list[dict[str, Any]], int]:
    """Rows on this bus belonging to `project` with id > since, plus the highest
    id scanned.

    The scanned-max is returned even for rows filtered out by project, so the
    puller advances its cursor past them and never re-scans them. Origin is not
    filtered: a message relayed from a third peer is served onward too, and guid
    dedup keeps it single on the receiver.
    """
    key = cloud.project_key(project)
    rows = conn.execute(
        f"SELECT {_MSG_COLS} FROM messages WHERE id>? ORDER BY id LIMIT ?",
        (since, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    max_id = since
    for r in rows:
        max_id = r["id"]
        if cloud.message_project(r["topic"], r["project"]) != key:
            continue
        d = dict(r)
        d["urgent"] = bool(d["urgent"])
        out.append(d)
    return out, max_id


def merge_incoming(conn, rows: list[dict[str, Any]], *, provenance: str = "lan") -> int:
    """Insert peer rows not already present (dedup by guid), stamped with
    ``provenance`` (``'lan'`` for the local wire, ``'wan'`` for a tunnelled peer),
    keeping their original guid + created_at so identity and ordering survive the
    hop. Returns the count newly inserted.
    """
    if not rows:
        return 0
    guids = [r.get("guid") for r in rows if r.get("guid")]
    have: set[str] = set()
    if guids:
        qs = ",".join("?" * len(guids))
        have = {
            g for (g,) in conn.execute(f"SELECT guid FROM messages WHERE guid IN ({qs})", guids)
        }
    n = 0
    for r in rows:
        g = r.get("guid")
        if not g or g in have:
            continue
        topic, to_session = r.get("topic"), r.get("to_session")
        if bool(topic) == bool(to_session):
            continue  # exactly one of topic / to_session must be set; skip malformed
        store.post_message(
            conn,
            from_session=r.get("from_session") or f"{provenance}:unknown",
            from_handle=r.get("from_handle"),
            body=r.get("body") or "",
            topic=topic,
            to_session=to_session,
            project=r.get("project"),
            urgent=bool(r.get("urgent")),
            provenance=provenance,
            guid=g,
            created_at=r.get("created_at"),
        )
        have.add(g)
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Wire framing — one JSON object per line over TCP.
# --------------------------------------------------------------------------- #
def _send_line(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode())


def _recv_line(sock: socket.socket) -> dict | None:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > _RECV_CAP:
            break
    line = buf.split(b"\n", 1)[0]
    if not line:
        return None
    try:
        return json.loads(line.decode())
    except Exception:
        return None


def handle_pull(conn, req: dict, project: str, secret: str | None) -> dict:
    """Serve one pull request read-only. Refuses wrong proto, a bad secret, or a
    request for a project this server is not sharing.
    """
    if req.get("proto") != PROTO:
        return {"ok": False, "error": "proto"}
    if secret and req.get("secret") != secret:
        return {"ok": False, "error": "unauthorized"}
    if cloud.project_key(req.get("project") or "") != cloud.project_key(project):
        return {"ok": False, "error": "project"}
    since = int(req.get("since") or 0)
    msgs, max_id = select_outbound(conn, project, since, PULL_LIMIT)
    return {"ok": True, "messages": msgs, "max_id": max_id, "node": node_id()}


# --------------------------------------------------------------------------- #
# Sockets — server (serves rows), client (pulls rows), UDP beacon + discovery.
# --------------------------------------------------------------------------- #
def open_server_socket(port: int = 0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Bind all interfaces on purpose: LAN peers connect from other hosts on the
    # subnet. The pull handshake is project-scoped and optionally secret-gated.
    s.bind(("0.0.0.0", port))  # noqa: S104
    s.listen(16)
    return s


def run_server(
    sock: socket.socket,
    project: str,
    conn_factory: Callable[[], Any],
    secret: str | None,
    stop: threading.Event,
) -> None:
    """Accept loop: one pull per connection, served from a fresh connection so
    the socket thread never shares a sqlite handle. Best-effort — a bad client
    never takes the server down.
    """
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            client, _ = sock.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        try:
            client.settimeout(_CONN_TIMEOUT)
            req = _recv_line(client)
            if req and req.get("op") == "pull":
                conn = conn_factory()
                try:
                    resp = handle_pull(conn, req, project, secret)
                finally:
                    conn.close()
                _send_line(client, resp)
        except Exception:
            pass
        finally:
            client.close()


def pull_from_peer(
    host: str, port: int, project: str, since: int, secret: str | None, node: str
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Pull a peer's project rows newer than `since`. Returns
    (rows, max_id, peer_node); on any failure returns ([], since, None) so the
    cursor holds and we retry later. `peer_node` is the serving peer's node id
    (handle_pull echoes it), which lets a caller detect a peer that restarted its
    DB and reset the per-peer cursor rather than stall above the new ids.
    """
    try:
        with socket.create_connection((host, port), timeout=_CONN_TIMEOUT) as s:
            s.settimeout(_CONN_TIMEOUT)
            _send_line(
                s,
                {
                    "op": "pull",
                    "proto": PROTO,
                    "project": project,
                    "since": since,
                    "secret": secret,
                    "node": node,
                },
            )
            resp = _recv_line(s)
    except OSError:
        return [], since, None
    if not resp or not resp.get("ok"):
        return [], since, None
    return resp.get("messages") or [], int(resp.get("max_id") or since), resp.get("node")


# --------------------------------------------------------------------------- #
# Paired sync: the daemon's LAN service (see pair.py for pairing and TLS).
# Whole bus, every paired peer, mutual TLS. A peer pulls on a 5 s floor, and
# right away when the other side sends a nudge after a new local message.
# --------------------------------------------------------------------------- #
SYNC_INTERVAL = 5.0
_WATCH_INTERVAL = 0.5
# The daemon's own session (daemon.DAEMON_SESSION). Its DMs are results for
# local agents, and a pair result holds a live PIN: never served to a peer.
_DAEMON_SESSION = "daemon"


def lan_settings() -> dict[str, Any]:
    """The ``[lan]`` table of the machine-wide ``~/.cabildo/config.toml``."""
    from . import config

    t = config._read(config._global_path()).get("lan")
    return t if isinstance(t, dict) else {}


def select_all(
    conn, since: int, exclude: set[str], limit: int = PULL_LIMIT
) -> tuple[list[dict[str, Any]], int]:
    """Rows with id > since, minus excluded projects, plus the highest id scanned.

    Excluded projects are filtered here, on the serving side, so they never leave
    this machine, as are the daemon's own DMs (a pair result holds a live PIN).
    The stored ``project`` path becomes its project key: paths differ between
    machines, repo names do not.
    """
    rows = conn.execute(
        f"SELECT {_MSG_COLS} FROM messages WHERE id>? ORDER BY id LIMIT ?",
        (since, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    max_id = since
    for r in rows:
        max_id = r["id"]
        if r["from_session"] == _DAEMON_SESSION:
            continue
        key = cloud.message_project(r["topic"], r["project"])
        if key and key in exclude:
            continue
        d = dict(r)
        d["urgent"] = bool(d["urgent"])
        d["project"] = key
        out.append(d)
    return out, max_id


class Service:
    """TLS server + pull loop for paired peers, run inside the daemon."""

    def __init__(self, me: Any, port: int, log: Callable[[str], None]) -> None:
        from . import pair

        self.me = me
        self.port = port
        self.log = log
        self.pairing = pair.Pairing()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._sock: socket.socket | None = None

    def exclude(self) -> set[str]:
        return {cloud.project_key(p) for p in lan_settings().get("exclude") or []}

    def start(self) -> bool:
        try:
            self._sock = open_server_socket(self.port)
        except OSError as e:
            self.log(f"lan: cannot listen on tcp/{self.port}: {e}")
            return False
        for fn in (self._serve, self._pull_loop, self._watch):
            threading.Thread(target=fn, daemon=True).start()
        return True

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        if self._sock is not None:
            self._sock.close()

    # server ------------------------------------------------------------- #
    def _serve(self) -> None:
        sock = self._sock
        if sock is None:
            return
        sock.settimeout(0.5)
        while not self.stop.is_set():
            try:
                raw, addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._one, args=(raw, addr[0]), daemon=True).start()

    def _one(self, raw: socket.socket, host: str) -> None:
        from . import pair

        try:
            raw.settimeout(_CONN_TIMEOUT)
            peers = pair.load_peers()
            with pair.server_context(self.me, peers).wrap_socket(raw, server_side=True) as s:
                req = pair.recv_line(s) or {}
                op = req.get("op")
                if op == "pair":
                    peer = pair.answer_pair(self.me, self.pairing, s, req, host)
                    if peer is not None:
                        self.log(f"lan: paired with {peer.node[:8]} at {host}")
                        self.wake.set()
                    return
                fp = pair.peer_fingerprint(s)
                known = {p.fingerprint: p for p in peers.values()}
                if fp not in known:
                    pair.send_line(s, {"ok": False, "error": "not paired"})
                    return
                if op == "pull":
                    since = int(req.get("since") or 0)
                    conn = store.connect()
                    try:
                        msgs, max_id = select_all(conn, since, self.exclude())
                        top = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()
                    finally:
                        conn.close()
                    pair.send_line(
                        s, {"ok": True, "messages": msgs, "max_id": max_id, "top": int(top[0])}
                    )
                elif op == "nudge":
                    self.wake.set()
                    pair.send_line(s, {"ok": True})
        except Exception as e:
            self.log(f"lan: connection from {host}: {type(e).__name__}: {e}")
        finally:
            raw.close()

    # client ------------------------------------------------------------- #
    def _cursor_path(self) -> Path:
        from . import pair

        return pair.lan_dir() / "cursor.json"

    def _pull_loop(self) -> None:
        while not self.stop.is_set():
            self.wake.wait(SYNC_INTERVAL)
            self.wake.clear()
            if self.stop.is_set():
                break
            try:
                self.sync_once()
            except Exception as e:
                self.log(f"lan: sync: {type(e).__name__}: {e}")

    def sync_once(self) -> int:
        """Pull from every paired peer once. Returns messages merged."""
        from . import pair

        try:
            cur: dict[str, int] = json.loads(self._cursor_path().read_text())
        except (OSError, ValueError):
            cur = {}
        merged = 0
        for peer in pair.load_peers().values():
            since = int(cur.get(peer.node, 0))
            try:
                with pair.connect(self.me, peer.host, peer.port, peer) as s:
                    pair.send_line(s, {"op": "pull", "proto": pair.PROTO, "since": since})
                    resp = pair.recv_line(s) or {}
            except (OSError, ValueError):
                continue
            if not resp.get("ok"):
                continue
            if int(resp.get("top") or 0) < since:
                cur[peer.node] = 0  # the peer's bus was reset; start over, guid dedup
                continue
            rows = resp.get("messages") or []
            if rows:
                conn = store.connect()
                try:
                    merged += merge_incoming(conn, rows)
                finally:
                    conn.close()
            cur[peer.node] = int(resp.get("max_id") or since)
        self._cursor_path().write_text(json.dumps(cur))
        return merged

    def _watch(self) -> None:
        """Nudge paired peers when a local message is written."""
        from . import pair

        conn = store.connect()
        try:
            last = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM messages WHERE provenance='local'"
            ).fetchone()[0]
            while not self.stop.wait(_WATCH_INTERVAL):
                top = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM messages WHERE provenance='local'"
                ).fetchone()[0]
                if top <= last:
                    continue
                last = top
                for peer in pair.load_peers().values():
                    try:
                        with pair.connect(self.me, peer.host, peer.port, peer) as s:
                            pair.send_line(s, {"op": "nudge", "proto": pair.PROTO})
                            pair.recv_line(s)
                    except (OSError, ValueError):
                        pass
        finally:
            conn.close()


# The daemon's running service, for the ``pair`` action. None outside the daemon.
SERVICE: Service | None = None


def start_service(log: Callable[[str], None]) -> Service | None:
    """Start the LAN service unless ``[lan] enabled = false`` or there is no openssl."""
    global SERVICE
    from . import pair

    cfg = lan_settings()
    if cfg.get("enabled") is False:
        return None
    me = pair.load_identity()
    if me is None:
        log("lan: off (no openssl to make a certificate)")
        return None
    svc = Service(me, int(cfg.get("port") or pair.DEFAULT_PORT), log)
    if not svc.start():
        return None
    SERVICE = svc
    log(f"lan: node {me.node[:8]} on tcp/{svc.port}, {len(pair.load_peers())} paired peer(s)")
    return svc
