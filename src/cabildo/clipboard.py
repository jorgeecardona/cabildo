"""Copy text to the system clipboard, on Linux (Wayland or X11) and macOS.

The tool is chosen from the session, not from what is installed first: ``wl-copy`` under
Wayland, ``xclip`` or ``xsel`` under X11, ``pbcopy`` on macOS. The text goes to the clipboard of
the machine this process runs on, where every program of that user can read it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile

MAX_BYTES = 1_000_000
WAIT_SECONDS = 2.0


class ClipboardError(Exception):
    """No clipboard tool is usable, or it failed."""


def candidates() -> list[list[str]]:
    """The copy commands to try, best first, for this session. Empty when none can work.

    X11 tools come first when there is an X display: they exit as soon as they have the text,
    and XWayland passes it on to Wayland programs. ``wl-copy`` is for a session with no X.
    """
    if sys.platform == "darwin":
        return [["pbcopy"]]
    out: list[list[str]] = []
    if os.environ.get("DISPLAY"):
        out += [["xclip", "-selection", "clipboard", "-in"], ["xsel", "--clipboard", "--input"]]
    if os.environ.get("WAYLAND_DISPLAY"):
        out.append(["wl-copy"])
    return out


def copy(text: str) -> str:
    """Put ``text`` on the clipboard and return the name of the tool that did it.

    Raises:
        ClipboardError: the text is over 1 MB, the session has no display, or no tool is installed.
    """
    data = text.encode()
    if len(data) > MAX_BYTES:
        raise ClipboardError(f"{len(data)} bytes is over the {MAX_BYTES} byte limit")
    tried = candidates()
    if not tried:
        raise ClipboardError("no display: WAYLAND_DISPLAY and DISPLAY are both unset")
    found = [cmd for cmd in tried if shutil.which(cmd[0])]
    if not found:
        names = " or ".join(cmd[0] for cmd in tried)
        raise ClipboardError(f"install {names}: nothing here can write the clipboard")
    errors: list[str] = []
    for cmd in found:
        try:
            _run(cmd, data)
        except (OSError, subprocess.TimeoutExpired, ClipboardError) as exc:
            errors.append(f"{cmd[0]}: {exc}")
            continue
        return cmd[0]
    raise ClipboardError("; ".join(errors))


def _run(cmd: list[str], data: bytes) -> None:
    """Feed ``data`` to one tool and wait for it to exit.

    ``xclip`` and ``wl-copy`` leave a background process that owns the selection. It keeps any
    pipe we gave it open, so output goes to /dev/null and to a file, never to a pipe: reading a
    pipe until it closes would wait for that process.
    """
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
        )
        try:
            if proc.stdin is None:
                raise ClipboardError("no stdin pipe")
            proc.stdin.write(data)
            proc.stdin.close()
            proc.wait(timeout=WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            if cmd[0] == "wl-copy":
                return  # it keeps running to serve the selection; a compositor may delay it
            with contextlib.suppress(OSError):
                proc.kill()
            raise
        except OSError:
            with contextlib.suppress(OSError):
                proc.kill()
            raise
        if proc.returncode:
            err.seek(0)
            raise ClipboardError(
                err.read().decode(errors="replace").strip() or f"exit {proc.returncode}"
            )
