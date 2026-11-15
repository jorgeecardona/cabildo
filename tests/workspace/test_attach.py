"""``attach``: a reload keeps its Claudes, because nothing is closed and nothing moves.

These drive the mechanism directly against a real pseudo-terminal and a real child, with
no app and no ``exec``: a descriptor duplicated the way a detach does it, then a terminal
object built on it the way an attach does, and the child asked whether it is still there.
"""

from __future__ import annotations

import fcntl
import os
import pty
import signal
import subprocess
import time

import pytest

from cabildo.workspace import attach


def spawn_on_pty() -> tuple[int, subprocess.Popen]:
    """A child on its own pseudo-terminal, echoing what it is sent, like a pane's Claude."""
    master, slave = pty.openpty()
    child = subprocess.Popen(
        ["cat"], stdin=slave, stdout=slave, stderr=slave, start_new_session=True
    )
    os.close(slave)  # the child holds it now
    return master, child


def wait_for(check, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def read_until(fd: int, want: bytes, timeout: float = 3.0) -> bool:
    """True once ``want`` has been read from ``fd``. A non-blocking fd with nothing yet
    raises ``BlockingIOError``; that means wait, not fail.
    """
    seen = bytearray()

    def check() -> bool:
        try:
            seen.extend(os.read(fd, 4096))
        except BlockingIOError:
            return False
        return want in seen

    return wait_for(check, timeout)


@pytest.fixture
def child():
    master, proc = spawn_on_pty()
    yield master, proc
    for fd in (master,):
        try:
            os.close(fd)
        except OSError:
            pass
    proc.kill()
    proc.wait()


# --------------------------------------------------------------------------- #
# The descriptor survives, and so does the child
# --------------------------------------------------------------------------- #
def test_the_kept_descriptor_is_not_closed_by_exec(child):
    master, _ = child
    kept = attach.keep_across_exec(master)
    assert kept != master
    assert not fcntl.fcntl(kept, fcntl.F_GETFD) & fcntl.FD_CLOEXEC


def test_closing_the_original_does_not_hang_the_child_up(child):
    """A pseudo-terminal hangs up on its last master, not its first. That is the whole
    reason a detach can let the old terminal object go.
    """
    master, proc = child
    kept = attach.keep_across_exec(master)
    os.close(master)  # what dropping the old terminal object does
    time.sleep(0.1)
    assert proc.poll() is None  # still running, on the duplicate
    os.write(kept, b"still here\n")
    assert read_until(kept, b"still here")
    os.close(kept)


def test_closing_the_last_master_hangs_the_child_up(child):
    """And the other way: discarding an inherited descriptor must end its Claude, not
    leave it blocked on a terminal nobody reads.
    """
    master, proc = child
    kept = attach.keep_across_exec(master)
    os.close(master)
    attach.discard(kept)
    assert wait_for(lambda: proc.poll() is not None)


# --------------------------------------------------------------------------- #
# Rebuilding the terminal on the other side
# --------------------------------------------------------------------------- #
def test_an_attached_terminal_talks_to_the_child_that_was_already_running(child):
    master, proc = child
    kept = attach.keep_across_exec(master)
    os.close(master)
    term = attach.attached_pty(kept, 24, 80)
    term.write("hello from the new program\n")
    seen: list[str] = []

    def echoed() -> bool:
        seen.append(term.read(4096))
        return "hello from the new program" in "".join(seen)

    assert wait_for(echoed)
    assert proc.poll() is None
    term.close()


def test_an_attached_terminal_closes_like_a_spawned_one(child):
    """`close` is inherited whole, so a quit after a reload ends Claude exactly as a quit
    without one would.
    """
    master, proc = child
    kept = attach.keep_across_exec(master)
    os.close(master)
    term = attach.attached_pty(kept, 24, 80)
    term.close()
    assert wait_for(lambda: proc.poll() is not None)


def test_resizing_an_attached_terminal_signals_the_child(child):
    """The repaint nudge: a size change is how Claude is asked to draw its screen again."""
    master, _ = child
    kept = attach.keep_across_exec(master)
    os.close(master)
    term = attach.attached_pty(kept, 24, 80)
    term.resize(30, 100)
    import struct
    import termios

    packed = fcntl.ioctl(kept, termios.TIOCGWINSZ, b"\0" * 8)
    assert struct.unpack("HHHH", packed)[:2] == (30, 100)
    term.close()


# --------------------------------------------------------------------------- #
# Standing in for a child this program did not spawn
# --------------------------------------------------------------------------- #
def test_the_stand_in_reports_a_living_child(child):
    _, proc = child
    stand_in = attach.InheritedProcess(proc.pid)
    assert stand_in.poll() is None
    assert stand_in.pid == proc.pid


def test_the_stand_in_reports_an_ended_child_once_it_is_gone():
    proc = subprocess.Popen(["true"])
    stand_in = attach.InheritedProcess(proc.pid)
    assert wait_for(lambda: stand_in.poll() is not None)
    assert stand_in.poll() == 0  # and keeps saying so, without a second reap


def test_a_child_that_is_not_ours_is_not_claimed():
    """A stale handoff can name a pid this process never spawned. `alive` must not raise
    on it, and must not report a stranger's process as a pane that can be rebuilt.
    """
    assert attach.alive(os.getpid()) is True  # ourselves: running, not our child
    assert attach.alive(2**22 + 1234) is False


# --------------------------------------------------------------------------- #
# The handoff itself
# --------------------------------------------------------------------------- #
def handed(**over) -> attach.Handed:
    fields = {
        "fd": 3,
        "pid": 1,
        "pane_id": "pane-1",
        "session_id": "s1",
        "cwd": "/repo",
        "group": "g",
        "title": "alpha",
        "rows": 24,
        "cols": 80,
    }
    return attach.Handed(**(fields | over))


def test_a_handoff_survives_being_written_and_read():
    panes = [handed(), handed(fd=4, pid=2, session_id="s2")]
    assert attach.decode(attach.encode(panes)) == panes


def test_a_handoff_of_another_shape_is_ignored():
    """An older or newer program's blob starts the app clean rather than reading numbers
    that mean something else now.
    """
    assert attach.decode('{"format": 999, "panes": []}') == []
    assert attach.decode("not json") == []
    assert attach.decode(None) == []


def test_taking_the_handoff_removes_it(monkeypatch, child):
    """Read once: a second start must not attach to descriptor numbers that have since
    been handed out to something else entirely.
    """
    master, proc = child
    kept = attach.keep_across_exec(master)
    monkeypatch.setenv(attach.ENV, attach.encode([handed(fd=kept, pid=proc.pid)]))
    assert [p.fd for p in attach.take()] == [kept]
    assert attach.ENV not in os.environ
    assert attach.take() == []
    os.close(kept)


def test_a_pane_whose_claude_died_is_dropped_and_its_descriptor_closed(child):
    """Claude can exit during the restart. Attaching to its terminal would hold a
    descriptor open for a process that is gone.
    """
    master, proc = child
    kept = attach.keep_across_exec(master)
    os.close(master)
    proc.kill()
    proc.wait()
    assert attach.decode(attach.encode([handed(fd=kept, pid=proc.pid)]))  # it is in the blob
    os.environ[attach.ENV] = attach.encode([handed(fd=kept, pid=proc.pid)])
    assert attach.take() == []  # but not in what comes back
    with pytest.raises(OSError):
        os.fstat(kept)  # and its descriptor was closed, not leaked


def test_hangup_is_quiet_about_a_process_that_is_already_gone():
    attach.hangup(2**22 + 4321)  # must not raise


def test_discard_is_quiet_about_a_descriptor_that_is_already_closed():
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    attach.discard(r)  # must not raise


def test_a_hung_up_child_ends(child):
    master, proc = child
    attach.hangup(proc.pid)
    assert wait_for(lambda: proc.poll() is not None)
    assert proc.returncode in (0, -signal.SIGHUP)


# --------------------------------------------------------------------------- #
# Letting go of a terminal the event loop is watching
# --------------------------------------------------------------------------- #
def test_release_unwatches_the_descriptor_before_closing_it(child):
    """The asyncio error a reload showed on Linux. bittty arms `add_reader` before each
    read and disarms it in the callback, so a read cancelled mid-await leaves it armed;
    closing a descriptor the loop still watches is what the selector complains about.
    """
    import asyncio

    master, _ = child
    kept = attach.keep_across_exec(master)

    async def go() -> tuple[bool, bool]:
        loop = asyncio.get_running_loop()
        pty = attach.attached_pty(master, 24, 80)
        loop.add_reader(pty.master_fd, lambda: None)  # armed, as a cancelled read leaves it
        watched_before = pty.master_fd in loop._selector.get_map()
        attach.release(pty)
        return watched_before, pty.master_fd in (loop._selector.get_map() or {})

    before, after = asyncio.run(go())
    assert before is True
    assert after is False  # unwatched, so the loop never polls a closed descriptor
    with pytest.raises(OSError):
        os.fstat(master)  # and the original is closed, not left to the collector
    os.close(kept)


def test_release_does_not_hang_the_child_up(child):
    """It closes a master, so the duplicate must be what keeps the child alive."""
    import asyncio

    master, proc = child
    kept = attach.keep_across_exec(master)

    async def go() -> None:
        attach.release(attach.attached_pty(master, 24, 80))

    asyncio.run(go())
    time.sleep(0.1)
    assert proc.poll() is None
    os.write(kept, b"still here\n")
    assert read_until(kept, b"still here")
    os.close(kept)


def test_release_is_quiet_outside_an_event_loop(child):
    """A detach can run where no loop is current; it must still close the descriptor."""
    master, _ = child
    kept = attach.keep_across_exec(master)
    attach.release(attach.attached_pty(master, 24, 80))
    with pytest.raises(OSError):
        os.fstat(master)
    os.close(kept)
