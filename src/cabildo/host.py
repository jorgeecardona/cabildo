"""host.py: the process that owns Claude sessions in pseudo-terminals.

A Claude that the TUI starts ends when the TUI ends, because the TUI owns its pseudo-terminal.
This host owns the pseudo-terminal instead. The TUI, the console (``cabildo attach``) and an
editor are viewers: they connect to a unix socket, see the same screen, and can all type.
Closing a viewer never ends a session.

The host is its own process, not a thread of the daemon. A daemon restart would close every
master descriptor and hang every Claude up. ``host_client.ensure_running`` starts it on demand.

Wire format. A client opens the socket and sends one JSON line, ``{"op": ..., ...}``. The host
answers with one JSON line. Two ops carry more:

- ``adopt`` sends one file descriptor with the line (``SCM_RIGHTS``): the master of a
  pseudo-terminal whose Claude another process started.
- ``attach`` turns the connection into frames after the reply: one byte of kind, four bytes of
  length, then the payload. ``DATA`` is terminal bytes in both directions. ``RESIZE`` (viewer to
  host) is rows and columns as two big-endian 16-bit integers. ``EXIT`` (host to viewer) is the
  exit code as a big-endian signed 32-bit integer, or -1 when unknown.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace

from . import store

DATA = 0
RESIZE = 1
EXIT = 2

_FRAME = struct.Struct(">BI")
_SIZE = struct.Struct(">HH")
_CODE = struct.Struct(">i")
_WINSZ = struct.Struct("HHHH")

#: Runs in the child before Claude: make the pseudo-terminal (fd 0) its controlling terminal.
#: ``start_new_session`` makes the child a session leader but does not set a controlling
#: terminal, and without one Claude gets no job control and no hangup.
_WRAPPER = (
    "import fcntl, os, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)

_SUBMIT_DELAY = 0.3  # seconds between typed text and its Enter
_REPAINT_DELAY = 0.3  # seconds a child keeps the smaller size, so that it reads it
_VIEWER_SEND_TIMEOUT = 5.0  # a viewer that cannot take output for this long is dropped
_LINE_LIMIT = 1 << 20  # a request line longer than this is refused


class HostError(Exception):
    """A request the host refuses. The message goes back to the client."""


class HostRunning(Exception):
    """Another host already listens on the socket."""


def socket_path() -> str:
    """The host's unix socket, beside the bus DB, unless ``CABILDO_HOST_SOCKET`` is set."""
    return os.environ.get("CABILDO_HOST_SOCKET") or str(store.home() / "host.sock")


@dataclass(frozen=True)
class Record:
    """One hosted session, as the host and its viewers see it."""

    id: str
    cwd: str
    command: tuple[str, ...]
    pid: int  # the Claude process (the pty child), not a viewer
    rows: int
    cols: int
    started_at: float
    group: str = ""
    session_id: str = ""  # Claude's own session id, once a viewer has matched it
    adopted: bool = False  # started by another process; the host is not its parent
    parent: str = ""  # the bus session id of the agent that started it, if an agent did

    @staticmethod
    def from_wire(raw: dict) -> Record:
        """Rebuild a record from its JSON form."""
        return Record(**{**raw, "command": tuple(raw.get("command", ()))})


def pack_frame(kind: int, payload: bytes = b"") -> bytes:
    """One frame, ready to send."""
    return _FRAME.pack(kind, len(payload)) + payload


def read_exact(sock: socket.socket, n: int) -> bytes | None:
    """Exactly ``n`` bytes, or None when the peer closes first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """The next frame as ``(kind, payload)``, or None when the peer closed."""
    head = read_exact(sock, _FRAME.size)
    if head is None:
        return None
    kind, size = _FRAME.unpack(head)
    payload = read_exact(sock, size) if size else b""
    if payload is None:
        return None
    return kind, payload


def unpack_code(payload: bytes) -> int:
    """The exit code in an ``EXIT`` frame; -1 when unknown."""
    return _CODE.unpack(payload)[0] if len(payload) == _CODE.size else -1


def pack_size(rows: int, cols: int) -> bytes:
    """The payload of a ``RESIZE`` frame."""
    return _SIZE.pack(max(1, min(rows, 65535)), max(1, min(cols, 65535)))


def set_size(fd: int, rows: int, cols: int) -> None:
    """Set the window size of the terminal behind ``fd``. The child gets SIGWINCH if it changed."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, _WINSZ.pack(rows, cols, 0, 0))


class _Viewer:
    """One attached connection. Sends are serialised and bounded in time."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self._lock = threading.Lock()

    def close(self) -> None:
        """End the connection, so the viewer reads the end of it.

        ``close`` alone does not: the thread blocked reading the viewer's keys keeps the socket
        open.
        """
        with contextlib.suppress(OSError):
            self.conn.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.conn.close()

    def send(self, kind: int, payload: bytes) -> bool:
        """Send one frame. False when the viewer is gone or too slow.

        The time limit is kept here and not with ``settimeout``: the same socket reads the
        viewer's keys, and a socket timeout would drop a viewer that types nothing.
        """
        view = memoryview(pack_frame(kind, payload))
        with self._lock:
            deadline = time.monotonic() + _VIEWER_SEND_TIMEOUT
            while view:
                left = max(0.0, deadline - time.monotonic())
                try:
                    if not select.select([], [self.conn], [], left)[1]:
                        return False
                    view = view[self.conn.send(view, socket.MSG_DONTWAIT) :]
                except BlockingIOError:
                    continue
                except (OSError, ValueError):  # ValueError: another thread closed it
                    return False
            return True


class _Session:
    """The live side of a record: the master descriptor, the viewers, the child."""

    def __init__(self, record: Record, master: int, proc: subprocess.Popen[bytes] | None) -> None:
        self.record = record
        self.master = master
        self.proc = proc  # None for an adopted session: not our child
        self.viewers: list[_Viewer] = []
        self.lock = threading.Lock()
        self.ended = threading.Event()

    def broadcast(self, kind: int, payload: bytes) -> None:
        """Send a frame to every viewer and drop the ones that fail."""
        with self.lock:
            viewers = list(self.viewers)
        dead = [v for v in viewers if not v.send(kind, payload)]
        if dead:
            with self.lock:
                self.viewers = [v for v in self.viewers if v not in dead]
            for v in dead:
                v.close()

    def drop_viewers(self) -> None:
        """Close every viewer's connection."""
        with self.lock:
            viewers, self.viewers = self.viewers, []
        for v in viewers:
            v.close()

    def write(self, data: bytes) -> None:
        """Type into the terminal. The master may be non-blocking when it came from another process."""
        view = memoryview(data)
        while view and not self.ended.is_set():
            try:
                view = view[os.write(self.master, view) :]
            except BlockingIOError:
                select.select([], [self.master], [], 1.0)
            except OSError:
                return

    def resize(self, rows: int, cols: int) -> None:
        """Set the size; the most recent viewer wins."""
        with contextlib.suppress(OSError):
            set_size(self.master, rows, cols)
        self.record = replace(self.record, rows=rows, cols=cols)

    def repaint(self) -> None:
        """Make the child redraw: one row less, a pause, then the size again.

        Without the pause the child reads only the final size, sees no change and draws nothing.
        """
        with contextlib.suppress(OSError):
            set_size(self.master, max(1, self.record.rows - 1), self.record.cols)
        if not self.ended.wait(_REPAINT_DELAY):  # an ended session's descriptor is closed
            with contextlib.suppress(OSError):
                set_size(self.master, self.record.rows, self.record.cols)


class Host:
    """The table of hosted sessions and the socket server in front of it."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or socket_path()
        self.sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # --- sessions ------------------------------------------------------------------

    def _add(self, session: _Session) -> None:
        with self._lock:
            self.sessions[session.record.id] = session
        threading.Thread(target=self._pump, args=(session,), daemon=True).start()

    def _get(self, session_id: str) -> _Session:
        with self._lock:
            session = self.sessions.get(session_id)
        if session is None or session.ended.is_set():
            raise HostError(f"no hosted session {session_id!r}")
        return session

    def _pump(self, session: _Session) -> None:
        """Copy the child's output to every viewer until the terminal closes."""
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([session.master], [], [], 1.0)
                if not ready:
                    continue
                try:
                    data = os.read(session.master, 65536)
                except BlockingIOError:
                    continue
                except OSError:  # EIO: the last slave closed, the child is gone
                    break
                if not data:
                    break
                session.broadcast(DATA, data)
        finally:
            if not self._stop.is_set():  # a stopping host sends no exit: the session did not end
                self._end(session)

    def _end(self, session: _Session) -> None:
        session.ended.set()
        code = -1
        if session.proc is not None:
            try:
                code = session.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                threading.Thread(target=session.proc.wait, daemon=True).start()
        session.broadcast(EXIT, _CODE.pack(code))
        session.drop_viewers()
        with contextlib.suppress(OSError):
            os.close(session.master)
        with self._lock:
            self.sessions.pop(session.record.id, None)

    def start(self, req: dict) -> Record:
        """Start ``command`` in a new pseudo-terminal and host it."""
        command = tuple(str(c) for c in req.get("command") or ())
        if not command:
            raise HostError("start needs a command")
        cwd = str(req.get("cwd") or os.getcwd())
        if not os.path.isdir(cwd):
            raise HostError(f"no such directory: {cwd}")
        env = {str(k): str(v) for k, v in (req.get("env") or os.environ).items()}
        if shutil.which(command[0], path=env.get("PATH")) is None:
            raise HostError(f"command not found: {command[0]}")
        rows, cols = _size(req)
        master, slave = os.openpty()
        try:
            set_size(slave, rows, cols)
            proc = subprocess.Popen(
                [sys.executable, "-c", _WRAPPER, *command],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(master)
            raise HostError(str(exc)) from exc
        finally:
            os.close(slave)
        record = Record(
            id=uuid.uuid4().hex[:8],
            cwd=cwd,
            command=command,
            pid=proc.pid,
            rows=rows,
            cols=cols,
            started_at=time.time(),
            group=str(req.get("group") or ""),
            session_id=str(req.get("session_id") or ""),
            parent=str(req.get("parent") or ""),
        )
        self._add(_Session(record, master, proc))
        return record

    def adopt(self, req: dict, fds: list[int]) -> Record:
        """Take over a pseudo-terminal master another process started a Claude on."""
        if len(fds) != 1:
            for fd in fds:
                os.close(fd)
            raise HostError("adopt needs exactly one file descriptor")
        (master,) = fds
        pid = int(req.get("pid") or 0)
        if pid <= 0 or not _alive(pid):
            os.close(master)
            raise HostError(f"process {pid} is not running")
        rows, cols = _size(req)
        record = Record(
            id=uuid.uuid4().hex[:8],
            cwd=str(req.get("cwd") or ""),
            command=tuple(str(c) for c in req.get("command") or ()),
            pid=pid,
            rows=rows,
            cols=cols,
            started_at=time.time(),
            group=str(req.get("group") or ""),
            session_id=str(req.get("session_id") or ""),
            adopted=True,
        )
        self._add(_Session(record, master, None))
        return record

    def tag(self, req: dict) -> Record:
        """Set the Claude session id and group a viewer has matched to a hosted session."""
        session = self._get(str(req.get("id") or ""))
        changes = {k: str(req[k]) for k in ("session_id", "group") if req.get(k) is not None}
        session.record = replace(session.record, **changes)
        return session.record

    def type_in(self, req: dict) -> None:
        """Type text into a session as a person would, and press Enter unless told not to."""
        session = self._get(str(req.get("id") or ""))
        text = str(req.get("text") or "")
        data = text.encode()
        if "\n" in text:  # one paste, so the lines are not sent as separate messages
            data = b"\x1b[200~" + data + b"\x1b[201~"
        session.write(data)
        if req.get("submit", True):
            time.sleep(_SUBMIT_DELAY)  # text and Enter in one write read as a pasted newline
            session.write(b"\r")

    def shutdown(self, force: bool) -> dict:
        """Stop serving. Refuses while sessions run, unless ``force``: stopping hangs them up."""
        live = self.records()
        if live and not force:
            raise HostError(f"{len(live)} session(s) still run; stopping the host ends them")
        self.stop()
        return {"stopped": len(live)}

    def kill(self, req: dict) -> None:
        """Hang the session up, as closing its terminal would."""
        session = self._get(str(req.get("id") or ""))
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(session.record.pid), signal.SIGHUP)

    def records(self) -> list[Record]:
        """The live sessions, oldest first."""
        with self._lock:
            live = [s.record for s in self.sessions.values() if not s.ended.is_set()]
        return sorted(live, key=lambda r: r.started_at)

    # --- the socket ----------------------------------------------------------------

    def serve(self) -> None:
        """Listen until ``stop`` is called. Raises ``HostRunning`` if another host is up."""
        srv = self._bind()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except TimeoutError:
                    if not os.path.exists(self.path):  # unlinked: nobody can reach this host
                        break
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            with self._lock:
                sessions = list(self.sessions.values())
            for session in sessions:  # after the socket: a viewer that asks finds no host
                session.drop_viewers()

    def stop(self) -> None:
        """End the accept loop. Sessions are hung up when the process exits."""
        self._stop.set()

    def _bind(self) -> socket.socket:
        if os.path.exists(self.path):
            probe = socket.socket(socket.AF_UNIX)
            probe.settimeout(0.5)
            try:
                probe.connect(self.path)
            except OSError:
                os.unlink(self.path)  # stale: a crashed host left it
            else:
                raise HostRunning(self.path)
            finally:
                probe.close()
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.path)
        os.chmod(self.path, 0o600)
        srv.listen(16)
        srv.settimeout(1.0)
        return srv

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10.0)
            line, fds = _read_request(conn)
            try:
                req = json.loads(line)
                op = str(req["op"])
            except (ValueError, KeyError, TypeError):
                for fd in fds:
                    os.close(fd)
                _reply(conn, {"ok": False, "error": "malformed request"})
                return
            try:
                if op == "attach":
                    self._attach(conn, req)
                else:
                    _reply(conn, {"ok": True, **self._run(op, req, fds)})
            except HostError as exc:
                _reply(conn, {"ok": False, "error": str(exc)})
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _run(self, op: str, req: dict, fds: list[int]) -> dict:
        if op == "adopt":
            return {"session": asdict(self.adopt(req, fds))}
        for fd in fds:  # only adopt takes a descriptor
            os.close(fd)
        if op == "start":
            return {"session": asdict(self.start(req))}
        if op == "list":
            return {"sessions": [asdict(r) for r in self.records()]}
        if op == "tag":
            return {"session": asdict(self.tag(req))}
        if op == "shutdown":
            return self.shutdown(bool(req.get("force")))
        if op == "input":
            self.type_in(req)
            return {}
        if op == "kill":
            self.kill(req)
            return {}
        if op == "ping":
            from . import __version__

            return {"version": __version__, "pid": os.getpid()}
        raise HostError(f"unknown op {op!r}")

    def _attach(self, conn: socket.socket, req: dict) -> None:
        session = self._get(str(req.get("id") or ""))
        rows, cols = _size(req)
        _reply(conn, {"ok": True, "session": asdict(session.record)})
        conn.settimeout(None)  # a viewer may stay silent for hours
        viewer = _Viewer(conn)
        with session.lock:
            session.viewers.append(viewer)
        session.resize(rows, cols)
        session.repaint()
        try:
            while True:
                frame = read_frame(conn)
                if frame is None:
                    break
                kind, payload = frame
                if kind == DATA:
                    session.write(payload)
                elif kind == RESIZE and len(payload) == _SIZE.size:
                    session.resize(*_SIZE.unpack(payload))
        except OSError:
            pass
        finally:
            with session.lock:
                if viewer in session.viewers:
                    session.viewers.remove(viewer)


def _size(req: dict) -> tuple[int, int]:
    rows = int(req.get("rows") or 24)
    cols = int(req.get("cols") or 80)
    return max(1, min(rows, 65535)), max(1, min(cols, 65535))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_request(conn: socket.socket) -> tuple[bytes, list[int]]:
    """One request line and the descriptors that came with it."""
    data, fds, _flags, _addr = socket.recv_fds(conn, 1 << 16, 8)
    buf = bytearray(data)
    while b"\n" not in buf:
        if len(buf) > _LINE_LIMIT:
            raise OSError("request too long")
        chunk = conn.recv(1 << 16)
        if not chunk:
            break
        buf += chunk
    return bytes(buf).split(b"\n", 1)[0], fds


def _reply(conn: socket.socket, message: dict) -> None:
    conn.sendall(json.dumps(message).encode() + b"\n")


def serve_forever() -> int:
    """``cabildo host``: run until SIGTERM or SIGINT."""
    host = Host()
    signal.signal(signal.SIGTERM, lambda *_: host.stop())
    signal.signal(signal.SIGINT, lambda *_: host.stop())
    try:
        host.serve()
    except HostRunning:
        print(f"a host is already listening on {host.path}", file=sys.stderr)
        return 1
    return 0
