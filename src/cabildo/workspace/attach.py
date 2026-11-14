"""Keeping the hosted Claudes alive across a reload.

`R` already restarts the workspace in place: ``main`` ends with ``os.execv``, which
replaces the program while keeping the process id, the open file descriptors and the
child processes. What used to end the sessions was everything that happened *before*
that -- the app quit, every pane unmounted, and unmounting a pane closes its
pseudo-terminal, which hangs Claude up.

So nothing has to be moved anywhere. A reload now:

  1. **detaches** each pane: stop reading the pseudo-terminal, duplicate its master file
     descriptor onto one that survives ``exec``, and drop the terminal object *without*
     closing it, so no SIGHUP is sent and the child keeps running;
  2. writes what each pane was (descriptor, child pid, session, size) into an environment
     variable, which ``exec`` carries into the new program;
  3. **attaches** after the restart: builds a pseudo-terminal object around the inherited
     descriptor instead of opening a new one, and a stand-in for the child process that
     was never ours to spawn this time around.

Two things are lost and worth saying out loud. The pane's scrollback lived in the old
program's memory and is gone; attaching nudges the terminal size so Claude repaints its
screen, but what scrolled past is not recoverable. And a descriptor that reaches the new
program but cannot be attached must be **closed**, not leaked: an open master with nobody
reading it leaves Claude blocked on a full buffer, which is worse than the hangup it
would otherwise have got.

This leans on private API of bittty (the terminal engine under textual-tty), which is
pinned exactly for that reason. The seams used are ``Board.pty``/``Board.process``/
``Board.host`` and ``UnixPTY``'s descriptor fields.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
from dataclasses import asdict, dataclass

#: Where the handoff is written for ``exec`` to carry. An environment variable rather
#: than a file: it cannot be left behind by a crash, and it cannot be read by the next
#: run after a plain (non-reload) start, which is exactly the lifetime wanted.
ENV = "CABILDO_WORKSPACE_HANDOFF"

#: The format of that blob. A new program reading an older shape ignores it and starts
#: clean: the descriptors are then closed, so the sessions end rather than wedge.
FORMAT = 1


@dataclass(frozen=True)
class Handed:
    """One pane as it crosses the restart."""

    fd: int  # the inherited pseudo-terminal master
    pid: int  # the Claude it belongs to, still a child of this process id
    pane_id: str
    session_id: str
    cwd: str
    group: str
    title: str
    rows: int
    cols: int


def keep_across_exec(fd: int) -> int:
    """Copy ``fd`` onto a descriptor that ``exec`` does not close, and return it.

    The copy refers to the same open pseudo-terminal, so the original may be closed
    afterwards without hanging Claude up: a terminal is hung up when its *last* master
    descriptor closes, not its first.
    """
    kept = os.dup(fd)
    flags = fcntl.fcntl(kept, fcntl.F_GETFD)
    fcntl.fcntl(kept, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    return kept


def release(pty) -> None:
    """Let go of a pseudo-terminal without hanging its child up.

    A terminal object dropped as it stands leaves two things behind. The event loop is
    still watching the descriptor: bittty arms ``add_reader`` before each read and
    disarms it inside the callback, so a read cancelled mid-await leaves it armed. And
    the descriptor itself is closed later, by the garbage collector, at a moment nobody
    chose. A closed descriptor the loop is still watching is what makes asyncio complain
    on Linux, whose selector keeps its own map of them; kqueue drops them quietly, which
    is why macOS showed nothing. So: unregister, then close, in that order.

    Not ``pty.close()``, which signals the child's process group -- the one thing a
    detach must not do. Closing this descriptor is safe because the caller duplicated it
    first, and a pseudo-terminal hangs up on its last master, not its first.
    """
    with contextlib.suppress(RuntimeError, ValueError, OSError, KeyError):
        asyncio.get_running_loop().remove_reader(pty.master_fd)
    with contextlib.suppress(OSError):
        pty.master_file.close()


def encode(panes: list[Handed]) -> str:
    return json.dumps({"format": FORMAT, "panes": [asdict(p) for p in panes]})


def decode(blob: str | None) -> list[Handed]:
    """The panes in a handoff blob, or none when it is absent, unreadable or older."""
    if not blob:
        return []
    try:
        data = json.loads(blob)
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            return []
        return [Handed(**p) for p in data["panes"]]
    except (ValueError, TypeError, KeyError):
        return []


def put(panes: list[Handed]) -> None:
    """Write the handoff where ``exec`` will carry it, or clear it when there is none."""
    if panes:
        os.environ[ENV] = encode(panes)
    else:
        os.environ.pop(ENV, None)


def take() -> list[Handed]:
    """Read the handoff and remove it, so a later restart cannot reuse stale numbers."""
    panes = decode(os.environ.pop(ENV, None))
    return [p for p in panes if _usable(p)]


def _usable(pane: Handed) -> bool:
    """Whether this descriptor and child really arrived. A pane whose Claude died during
    the restart is dropped here, and its descriptor closed, rather than attached to a
    terminal nobody is on the other end of.
    """
    try:
        os.fstat(pane.fd)
    except OSError:
        return False
    if not alive(pane.pid):
        discard(pane.fd)
        return False
    return True


def alive(pid: int) -> bool:
    """Whether the process is still running. A zombie counts as gone: it is a child of
    this process id, so it is reaped here and reported dead.
    """
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        done = 0  # not our child (a cold start reading a stale handoff): fall through
    except OSError:
        return False
    if done == pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discard(fd: int) -> None:
    """Close an inherited descriptor that will not be attached.

    Closing it is the point: it is the last master of that pseudo-terminal, so Claude
    gets the hangup it would have got from a plain quit instead of blocking forever on a
    terminal nobody reads.
    """
    try:
        os.close(fd)
    except OSError:
        pass


def hangup(pid: int) -> None:
    """Hang up a child whose pane could not be rebuilt, so it does not outlive the app."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGHUP)
    except OSError:
        pass


class InheritedProcess:
    """A child this program did not spawn, with the little of ``Popen`` its owners use.

    After the restart the Claude processes are still children of this process id -- the
    same id, since ``exec`` keeps it -- but the ``Popen`` objects that wrapped them died
    with the previous program. The board and the pane want ``pid``, ``poll`` and
    ``wait``; this is those three over ``waitpid``.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """The exit status if it has ended, else None. Reaps, so no zombie is left."""
        if self.returncode is not None:
            return self.returncode
        try:
            done, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = 0  # already reaped elsewhere; gone either way
            return self.returncode
        except OSError:
            return None
        if done == self.pid:
            self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Block until it ends. ``timeout`` is accepted and ignored, as the callers
        here never pass one; a child being hung up ends at once.
        """
        if self.returncode is None:
            try:
                _, status = os.waitpid(self.pid, 0)
                self.returncode = os.waitstatus_to_exitcode(status)
            except (ChildProcessError, OSError):
                self.returncode = 0
        return self.returncode


def attached_pty(master_fd: int, rows: int, cols: int):
    """A bittty pseudo-terminal around an inherited master, instead of a new pair.

    ``UnixPTY.__init__`` always calls ``openpty``, so it is skipped and its grandparent
    is initialised directly with the descriptor that came through ``exec``. Everything
    else about the object is a real ``UnixPTY``, including ``close``, which is what makes
    a later quit hang Claude up exactly as a freshly spawned pane would.
    """
    from bittty.pty.base import PTY
    from bittty.pty.unix import UnixPTY

    pty = UnixPTY.__new__(UnixPTY)
    pty.master_fd = master_fd
    pty.slave_fd = None  # the slave belongs to the child; this program never held it
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    pty.master_file = os.fdopen(master_fd, "rb+", buffering=0)
    PTY.__init__(pty, pty.master_file, pty.master_file, rows, cols)
    pty.resize(rows, cols)
    return pty
