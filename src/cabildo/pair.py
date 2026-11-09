"""LAN pairing and TLS: two machines pair once with a 6-digit PIN, then sync over
mutual TLS with each other's pinned certificate.

Flow:

1. Machine A: ``request("pair")``. The daemon mints a PIN, opens a 120 s pairing
   window, and marks its mDNS announcement (``mdns.py``) as pairing.
2. Machine B: ``request("pair", {"pin": "482913"})``. The daemon finds the one
   machine announcing a pairing over mDNS (or uses ``host`` + ``port`` if given), connects
   over TLS, and both sides prove the PIN with an HMAC over both certificate
   fingerprints and both nonces.
3. Each side stores the other's certificate. Every later sync is mutual TLS
   against the stored certificates. The PIN is never used again.

The PIN is not a secret against the network: it keeps agents of different projects
from pairing by accident, on a network assumed to be safe. The window lasts 10
minutes and closes on a right PIN or after 5 wrong ones. The announcement carries
no hash of the PIN, since a 6-digit PIN behind a hash is guessed in milliseconds.

Certificates are made once with the ``openssl`` CLI (stdlib ``ssl`` cannot make
them). Without ``openssl`` the LAN service stays off.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import store

PROTO = 2
# The PIN only keeps projects apart on a network that is assumed safe, so the window is
# long enough to relay it between two Claude sessions, and a typo is not fatal.
PAIR_WINDOW = 600.0
MAX_TRIES = 5
_TIMEOUT = 10.0
_RECV_CAP = 16 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Identity:
    """This machine's node id and TLS certificate."""

    node: str
    cert_path: Path
    key_path: Path
    cert_pem: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.cert_pem)


@dataclass(frozen=True)
class PairedPeer:
    """A machine this one paired with, and where to reach it."""

    node: str
    host: str
    port: int
    cert_pem: str
    paired_at: float

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.cert_pem)


@dataclass(frozen=True)
class Window:
    """An open pairing window on this machine. One attempt, then it closes."""

    pin: str
    nonce: str
    expires_at: float


def lan_dir() -> Path:
    return store.home() / "lan"


def fingerprint(cert_pem: str) -> str:
    """The sha256 of the certificate's DER bytes, hex."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest()


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def load_identity() -> Identity | None:
    """This machine's identity, made on first call. None without ``openssl``."""
    d = lan_dir()
    cert, key, node_file = d / "node.crt", d / "node.key", d / "node.json"
    if not (cert.exists() and key.exists() and node_file.exists()):
        if not openssl_available():
            return None
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        node = secrets.token_hex(16)
        rc = subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-days",
                "3650",
                "-subj",
                f"/CN=cabildo-{node}",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            capture_output=True,
            check=False,
        ).returncode
        if rc != 0:
            return None
        key.chmod(0o600)
        node_file.write_text(json.dumps({"node": node}))
    node = str(json.loads(node_file.read_text())["node"])
    return Identity(node=node, cert_path=cert, key_path=key, cert_pem=cert.read_text())


def _peers_file() -> Path:
    return lan_dir() / "peers.json"


def load_peers() -> dict[str, PairedPeer]:
    try:
        raw = json.loads(_peers_file().read_text())
    except (OSError, ValueError):
        return {}
    return {p["node"]: PairedPeer(**p) for p in raw}


def save_peer(peer: PairedPeer) -> None:
    peers = load_peers()
    peers[peer.node] = peer
    _write_peers(peers)


def _write_peers(peers: dict[str, PairedPeer]) -> None:
    lan_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = _peers_file().with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(p) for p in peers.values()], indent=1))
    tmp.chmod(0o600)
    tmp.replace(_peers_file())


# --------------------------------------------------------------------------- #
# Pure pairing logic
# --------------------------------------------------------------------------- #
def new_pin() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def tag(pin: str, role: str, fp_a: str, fp_b: str, nonce_a: str, nonce_b: str) -> str:
    """The HMAC that proves the PIN. ``role`` is ``"b"`` for the joiner, ``"a"`` for
    the answer, so one side's tag cannot be replayed as the other's.
    """
    key = hashlib.sha256(pin.encode()).digest()
    msg = f"cabildo-pair|{role}|{fp_a}|{fp_b}|{nonce_a}|{nonce_b}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class Pairing:
    """The pairing window of the running daemon. Thread-safe; one window at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window: Window | None = None
        self._fails = 0

    def open(self, now: float | None = None) -> Window:
        w = Window(
            pin=new_pin(),
            nonce=secrets.token_hex(16),
            expires_at=(now or time.time()) + PAIR_WINDOW,
        )
        with self._lock:
            self._window = w
            self._fails = 0
        return w

    def live(self, now: float | None = None) -> Window | None:
        with self._lock:
            w = self._window
            if w is not None and (now or time.time()) >= w.expires_at:
                self._window = w = None
            return w

    def fail(self, w: Window) -> int:
        """Count a wrong PIN against ``w``; the window closes at ``MAX_TRIES``.
        Returns the tries left (0 once closed).
        """
        with self._lock:
            if self._window is not w:
                return 0
            self._fails += 1
            left = MAX_TRIES - self._fails
            if left <= 0:
                self._window = None
            return max(left, 0)

    def close(self, w: Window | None = None) -> None:
        """Close the window, or only ``w`` if it is still the live one."""
        with self._lock:
            if w is None or self._window is w:
                self._window = None


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #
def server_context(me: Identity, peers: dict[str, PairedPeer]) -> ssl.SSLContext:
    """Server side. A client cert is optional (a joiner has none yet) but, when
    sent, must be one of the paired certificates.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(me.cert_path, me.key_path)
    if peers:
        ctx.verify_mode = ssl.CERT_OPTIONAL
        ctx.load_verify_locations(cadata="".join(p.cert_pem for p in peers.values()))
    return ctx


def client_context(me: Identity, peer: PairedPeer | None) -> ssl.SSLContext:
    """Client side. With ``peer``, only its pinned certificate is accepted. Without,
    any certificate is accepted (pairing), and the PIN tag binds its fingerprint.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    if peer is None:
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.load_cert_chain(me.cert_path, me.key_path)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=peer.cert_pem)
    return ctx


def peer_fingerprint(sock: ssl.SSLSocket) -> str | None:
    der = sock.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest() if der else None


def send_line(sock: socket.socket, obj: dict[str, Any]) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode())


def recv_line(sock: socket.socket) -> dict[str, Any] | None:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > _RECV_CAP:
            return None
    line = buf.split(b"\n", 1)[0]
    try:
        obj = json.loads(line.decode()) if line else None
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def connect(me: Identity, host: str, port: int, peer: PairedPeer | None) -> ssl.SSLSocket:
    """A TLS connection to ``host:port``. With ``peer``, the server must present
    exactly its pinned certificate.
    """
    raw = socket.create_connection((host, port), timeout=_TIMEOUT)
    try:
        s = client_context(me, peer).wrap_socket(raw)
    except Exception:
        raw.close()
        raise
    s.settimeout(_TIMEOUT)
    if peer is not None and peer_fingerprint(s) != peer.fingerprint:
        s.close()
        raise ssl.SSLError("peer certificate does not match the paired one")
    return s


# --------------------------------------------------------------------------- #
# Pairing over the wire
# --------------------------------------------------------------------------- #
def answer_pair(
    me: Identity,
    pairing: Pairing,
    sock: ssl.SSLSocket,
    req: dict[str, Any],
    host: str,
    log: Callable[[str], None] = lambda _m: None,
) -> PairedPeer | None:
    """Server half (machine A). Returns the new peer on success.

    A malformed request is refused without touching the window. A wrong PIN counts
    one try, and the window closes after ``MAX_TRIES``; a right one closes it.
    """
    cert_b, nonce_b = str(req.get("cert") or ""), str(req.get("nonce") or "")
    node_b = str(req.get("node") or "")
    try:
        fp_b = fingerprint(cert_b)
    except ValueError:
        fp_b = ""
    if not (fp_b and nonce_b and node_b and int(req.get("port") or 0) > 0):
        send_line(sock, {"ok": False, "error": "malformed pair request"})
        return None
    w = pairing.live()
    if w is None:
        log(f"lan: pair attempt from {host}: no window open")
        send_line(sock, {"ok": False, "error": "no pairing window open"})
        return None
    send_line(sock, {"ok": True, "node": me.node, "cert": me.cert_pem, "nonce": w.nonce})
    proof = recv_line(sock) or {}
    want = tag(w.pin, "b", me.fingerprint, fp_b, w.nonce, nonce_b)
    if not hmac.compare_digest(str(proof.get("tag") or ""), want):
        left = pairing.fail(w)
        log(f"lan: pair attempt from {host}: wrong PIN, {left} tries left")
        send_line(sock, {"ok": False, "error": f"wrong PIN ({left} tries left)"})
        return None
    peer = PairedPeer(
        node=node_b,
        host=host,
        port=int(req.get("port") or 0),
        cert_pem=cert_b,
        paired_at=time.time(),
    )
    # Save before the final "ok": B syncs as soon as it reads it, and A must know B by then.
    save_peer(peer)
    send_line(sock, {"ok": True, "tag": tag(w.pin, "a", me.fingerprint, fp_b, w.nonce, nonce_b)})
    pairing.close(w)
    return peer


def join(me: Identity, host: str, port: int, pin: str, my_port: int) -> PairedPeer:
    """Client half (machine B). Raises ``PairError`` with a reason on failure."""
    nonce_b = secrets.token_hex(16)
    try:
        s = connect(me, host, port, None)
    except OSError as e:
        raise Unreachable(f"cannot reach {host}:{port}: {e}") from e
    with s:
        fp_a_seen = peer_fingerprint(s) or ""
        send_line(
            s,
            {
                "op": "pair",
                "proto": PROTO,
                "node": me.node,
                "port": my_port,
                "cert": me.cert_pem,
                "nonce": nonce_b,
            },
        )
        hello = recv_line(s) or {}
        if not hello.get("ok"):
            raise PairError(str(hello.get("error") or "no answer"))
        cert_a, node_a = str(hello.get("cert") or ""), str(hello.get("node") or "")
        if not node_a:
            raise PairError("the other machine sent no node id")
        if fingerprint(cert_a) != fp_a_seen:
            raise PairError("certificate sent does not match the TLS certificate")
        nonce_a = str(hello.get("nonce") or "")
        send_line(s, {"tag": tag(pin, "b", fp_a_seen, me.fingerprint, nonce_a, nonce_b)})
        done = recv_line(s) or {}
        if not done.get("ok"):
            raise PairError(str(done.get("error") or "refused"))
        want = tag(pin, "a", fp_a_seen, me.fingerprint, nonce_a, nonce_b)
        if not hmac.compare_digest(str(done.get("tag") or ""), want):
            raise PairError("the other machine could not prove the PIN")
    peer = PairedPeer(node=node_a, host=host, port=port, cert_pem=cert_a, paired_at=time.time())
    save_peer(peer)
    return peer


class PairError(Exception):
    """Pairing failed; the message says why, in words for the user."""


class Unreachable(PairError):
    """Nothing answered at that address; the PIN was not used."""
