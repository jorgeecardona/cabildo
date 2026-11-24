"""Ask a running workspace to show one conversation ("take me to xxx").

A workspace registers a folder ``<runtime>/workspace/focus/<pid>/``. The ``sessions_focus``
MCP tool writes ``request.json`` into the folder of every live workspace and waits for
``reply.json``. Each workspace looks the name up in its own list and answers, so the tool can
say which one showed it, or why none did.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from cabildo.workspace import xdgdirs

REPLY_SECONDS = 5.0  # how long the tool waits for the workspaces to answer
POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Request:
    """What to show: the id pairs the reply with the request."""

    id: str
    name: str


@dataclass(frozen=True)
class Reply:
    """A workspace's answer: ``ok`` when it now shows the conversation."""

    id: str
    ok: bool
    message: str
    pid: int


def root() -> Path:
    """The folder that holds one subfolder per live workspace."""
    return xdgdirs.runtime_home() / xdgdirs.APP / "focus"


def register(pid: int | None = None) -> Path:
    """Make this workspace's folder (idempotent) and return it."""
    folder = root() / str(pid or os.getpid())
    xdgdirs.make_dirs(folder, xdgdirs.runtime_home())
    return folder


def unregister(folder: Path) -> None:
    """Remove the folder with its files; a missing one is fine."""
    for f in folder.glob("*"):
        with contextlib.suppress(OSError):
            f.unlink()
    with contextlib.suppress(OSError):
        folder.rmdir()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def instances() -> list[Path]:
    """The folders of the live workspaces. Folders of dead processes are removed."""
    out: list[Path] = []
    if not root().is_dir():
        return out
    for folder in sorted(root().iterdir()):
        if folder.name.isdigit() and _alive(int(folder.name)):
            out.append(folder)
        elif folder.name.isdigit():
            unregister(folder)
    return out


def _write(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)  # the reader never sees half a file


def take(folder: Path) -> Request | None:
    """The pending request of this workspace, removed from the folder; None when there is none."""
    path = folder / "request.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        return Request(id=str(data["id"]), name=str(data["name"]))
    except (OSError, ValueError, KeyError):
        return None


def answer(folder: Path, request: Request, *, ok: bool, message: str) -> None:
    """Write this workspace's reply to ``request``."""
    reply = Reply(id=request.id, ok=ok, message=message, pid=int(folder.name))
    _write(folder / "reply.json", asdict(reply))


def ask(name: str, timeout: float = REPLY_SECONDS) -> list[Reply]:
    """Send ``name`` to every live workspace and collect the replies that come in time.

    Returns an empty list when no workspace runs. A workspace that does not answer within
    ``timeout`` seconds is left out.
    """
    request = Request(id=uuid.uuid4().hex, name=name)
    waiting = instances()
    for folder in waiting:
        (folder / "reply.json").unlink(missing_ok=True)
        _write(folder / "request.json", asdict(request))
    replies: list[Reply] = []
    deadline = time.monotonic() + timeout
    while waiting and time.monotonic() < deadline:
        for folder in list(waiting):
            try:
                data = json.loads((folder / "reply.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("id") != request.id:
                continue
            replies.append(Reply(**data))
            (folder / "reply.json").unlink(missing_ok=True)
            waiting.remove(folder)
        if waiting:
            time.sleep(POLL_SECONDS)
    for folder in waiting:  # nobody read it: do not leave it for a later start
        (folder / "request.json").unlink(missing_ok=True)
    return replies
