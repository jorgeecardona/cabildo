"""host_client.py: talk to the session host (``host.py``) and attach a terminal to a session.

``start`` and ``adopt`` put a session under the host. ``attach`` connects the calling terminal to
one. All of them start the host first if no host is listening.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Mapping, Sequence

from . import host, store

#: Variables a running Claude sets for the processes it starts. They name that Claude's own
#: session, channel and token. A Claude started with them thinks it is a child of the first one:
#: it turns transcript saving off ("inherited CLAUDE_CODE_CHILD_SESSION marker") and could talk
#: on the first one's channel. Settings the user chose (``CLAUDE_CONFIG_DIR``, the model, the
#: provider flags) are not on this list.
SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_CHILD_NOTES_DIR",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
)


def clean_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``env`` (default: this process's) without the variables of the Claude that runs it."""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if k not in SESSION_VARS}


#: Exit code of ``attach`` when the connection to the host closes before the session ends.
DISCONNECTED = os.EX_TEMPFAIL

#: Turns off what the session may have turned on in this terminal (alternate screen, hidden
#: cursor, mouse and focus reports, bracketed paste, extended keys), so the message after a lost
#: connection is readable and Enter reads as Enter.
_RESET_TERMINAL = (
    b"\x1b[?1047l\x1b[?25h\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2004l"
    b"\x1b[<u\x1b[>4m\x1b[0m\r\n"
)


class ClientError(Exception):
    """The host refused the request, or none could be started."""


def _connect(path: str | None = None) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(5.0)
    sock.connect(path or host.socket_path())
    return sock


def _read_line(sock: socket.socket) -> dict:
    """One JSON line, read a byte at a time so nothing after it is consumed (frames follow)."""
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise ClientError("the host closed the connection")
        buf += chunk
    return json.loads(bytes(buf))


def is_running() -> bool:
    """Whether a host answers on the socket."""
    try:
        reply = request("ping", start=False)
    except (ClientError, OSError):
        return False
    return bool(reply.get("ok"))


def ping() -> dict | None:
    """The host's answer (``version``, ``pid``), or None when none runs. Starts nothing."""
    try:
        return request("ping", start=False)
    except (ClientError, OSError):
        return None


def ensure_running(timeout: float = 5.0) -> None:
    """Start the host detached if nothing listens, and wait until it answers.

    Raises:
        ClientError: when it does not answer within ``timeout`` seconds.
    """
    if is_running():
        return
    log = store.home() / "host.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        subprocess.Popen(
            [sys.executable, "-m", "cabildo.cli", "host"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,  # survives the process that needed it
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return
        time.sleep(0.05)
    raise ClientError(f"the session host did not start (see {log})")


def request(op: str, *, fds: Sequence[int] = (), start: bool = True, **params: object) -> dict:
    """Send one request and return the reply. Raises ``ClientError`` on ``ok: false``."""
    if start:
        ensure_running()
    try:
        sock = _connect()
    except OSError as exc:
        raise ClientError(f"no session host: {exc}") from exc
    with sock:
        line = json.dumps({"op": op, **params}).encode() + b"\n"
        if fds:
            socket.send_fds(sock, [line], list(fds))
        else:
            sock.sendall(line)
        reply = _read_line(sock)
    if not reply.get("ok"):
        error = str(reply.get("error") or "the host refused the request")
        if error.startswith("unknown op"):
            error += " (the host runs an older cabildo; `cabildo host stop` ends it)"
        raise ClientError(error)
    return reply


def start_session(
    command: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    rows: int = 24,
    cols: int = 80,
    group: str = "",
    session_id: str = "",
    parent: str = "",
) -> host.Record:
    """Run ``command`` in ``cwd`` under the host, with ``env`` (default: this process's).

    The variables in ``SESSION_VARS`` are dropped from it, so a session started from inside
    another Claude is not taken for a child of that one.
    """
    reply = request(
        "start",
        command=list(command),
        cwd=cwd,
        env=clean_env(env),
        rows=rows,
        cols=cols,
        group=group,
        session_id=session_id,
        parent=parent,
    )
    return host.Record.from_wire(reply["session"])


def adopt_session(
    master_fd: int,
    pid: int,
    *,
    cwd: str = "",
    rows: int = 24,
    cols: int = 80,
    group: str = "",
    session_id: str = "",
) -> host.Record:
    """Give the host a running Claude: its pseudo-terminal master and its process id.

    The host gets its own copy of the descriptor, so the caller may close ``master_fd`` after
    this returns.
    """
    reply = request(
        "adopt",
        fds=[master_fd],
        pid=pid,
        cwd=cwd,
        rows=rows,
        cols=cols,
        group=group,
        session_id=session_id,
    )
    return host.Record.from_wire(reply["session"])


def list_sessions() -> list[host.Record]:
    """The live hosted sessions. Empty when no host runs (it is not started for this)."""
    try:
        reply = request("list", start=False)
    except (ClientError, OSError):
        return []
    return [host.Record.from_wire(r) for r in reply["sessions"]]


def tag_session(host_id: str, *, session_id: str | None = None, group: str | None = None) -> None:
    """Record the Claude session id and group a viewer matched to a hosted session."""
    request("tag", id=host_id, session_id=session_id, group=group)


def send_input(host_id: str, text: str, *, submit: bool = True) -> None:
    """Type ``text`` into a hosted session; press Enter after it when ``submit`` is true."""
    request("input", id=host_id, text=text, submit=submit, start=False)


def stop_host(*, force: bool = False) -> int:
    """Stop the host; the count of sessions it ended. Refused while sessions run, unless forced."""
    return int(request("shutdown", force=force, start=False).get("stopped", 0))


def kill_session(host_id: str) -> None:
    """Hang a hosted session up."""
    request("kill", id=host_id, start=False)


def attach_command(host_id: str) -> tuple[str, ...]:
    """The command a terminal pane runs to show the hosted session ``host_id``.

    It names the socket, so the viewer reaches the host this process uses even when its own
    environment differs (a terminal library that copied the environment earlier).
    """
    return (sys.executable, "-m", "cabildo.cli", "attach", "--socket", host.socket_path(), host_id)


def attach(host_id: str, *, hold: bool = False) -> int:
    """Connect this terminal to a hosted session until it ends or the terminal closes.

    Returns the session's exit code, 0 when this terminal closed first, 1 when the host refuses,
    or ``DISCONNECTED`` when the connection closes before the session ends. The last two print
    why. With ``hold``, they then wait for Enter and return 0, so a terminal tab that closes on
    exit keeps the message on screen.
    """
    try:
        ensure_running()
        size = shutil.get_terminal_size()
        sock = _connect()
        sock.sendall(
            json.dumps(
                {"op": "attach", "id": host_id, "rows": size.lines, "cols": size.columns}
            ).encode()
            + b"\n"
        )
        reply = _read_line(sock)
    except (OSError, ClientError) as exc:
        return _fail(str(exc), 1, hold)
    if not reply.get("ok"):
        return _fail(str(reply.get("error")), 1, hold)
    sock.settimeout(None)
    code = _relay(sock)
    if code is None:
        if os.isatty(sys.stdout.fileno()):
            os.write(sys.stdout.fileno(), _RESET_TERMINAL)
        return _fail(_why_disconnected(host_id), DISCONNECTED, hold)
    return code


def _why_disconnected(host_id: str) -> str:
    """What the person can do after the connection to the host closed."""
    if not is_running():
        return "the session host stopped. Its sessions ended with it."
    if any(r.id == host_id for r in list_sessions()):
        return f"the host closed this viewer. The session still runs: cabildo attach {host_id}"
    return f"the connection closed and the host no longer has session {host_id}."


def _fail(message: str, code: int, hold: bool) -> int:
    """Print why ``attach`` stops; wait for Enter first when ``hold`` and a person can press it."""
    print(f"cabildo attach: {message}", file=sys.stderr)
    if not (hold and sys.stdin.isatty()):
        return code
    print("Press Enter to close.", file=sys.stderr, flush=True)
    with contextlib.suppress(OSError, KeyboardInterrupt):
        sys.stdin.readline()
    return 0


def _relay(sock: socket.socket) -> int | None:
    """Copy keys to the host and the host's output to the screen; follow window resizes.

    Returns the session's exit code, 0 when this terminal closed first, or None when the
    connection to the host closed before either.
    """
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    is_tty = os.isatty(stdin)
    saved = termios.tcgetattr(stdin) if is_tty else None
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    previous = signal.signal(signal.SIGWINCH, lambda *_: _poke(wake_w))
    try:
        if is_tty:
            tty.setraw(stdin)
        while True:
            ready, _, _ = select.select([stdin, sock, wake_r], [], [])
            if wake_r in ready:
                os.read(wake_r, 4096)
                size = shutil.get_terminal_size()
                if not _send(sock, host.RESIZE, host.pack_size(size.lines, size.columns)):
                    return None
            if stdin in ready:
                data = os.read(stdin, 65536)
                if not data:
                    return 0
                if not _send(sock, host.DATA, data):
                    return None
            if sock in ready:
                frame = _receive(sock)
                if frame is None:
                    return None
                kind, payload = frame
                if kind == host.DATA:
                    _write_all(stdout, payload)
                elif kind == host.EXIT:
                    return max(host.unpack_code(payload), 0)
    except OSError:  # this terminal is gone
        return 0
    finally:
        signal.signal(signal.SIGWINCH, previous)
        if saved is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
        with contextlib.suppress(OSError):
            sock.close()
        os.close(wake_r)
        os.close(wake_w)


def _send(sock: socket.socket, kind: int, payload: bytes) -> bool:
    """Send one frame to the host. False when the connection is closed."""
    try:
        sock.sendall(host.pack_frame(kind, payload))
    except OSError:
        return False
    return True


def _receive(sock: socket.socket) -> tuple[int, bytes] | None:
    """The host's next frame, or None when the connection is closed."""
    try:
        return host.read_frame(sock)
    except OSError:
        return None


def _poke(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.write(fd, b"x")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
