"""What the app had open, kept so the next start (or the start after a crash) has it again.

Pure stdlib. One small JSON file per list directory, under the XDG state directory
($XDG_STATE_HOME, default ~/.local/state: "current state of the application that can
be reused on a restart"). Written on every change, atomically, so it is current even
when the computer dies.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from cabildo.workspace.sessions import NO_FILTER, Filter, encode_cwd
from cabildo.workspace.xdgdirs import APP, make_dirs, state_home


@dataclass(frozen=True)
class OpenState:
    """The conversations open in the app for one list directory."""

    cwd: str  # the list directory (the app's own)
    all_dirs: bool = False  # the list showed every directory (`a`)
    filter: Filter = NO_FILTER  # the list showed only these groups / tags (`f`, -G, -t)
    open: tuple[str, ...] = ()  # session ids of the open conversations, in opening order
    shown: str | None = None  # the one in the main pane


def open_dir() -> Path:
    return state_home() / APP / "open"


def state_path(cwd: str) -> Path:
    return open_dir() / f"{encode_cwd(cwd)}.json"


def load(cwd: str) -> OpenState:
    """The saved state of this list directory; an empty one when there is none or it
    cannot be read (it is only a convenience).
    """
    empty = OpenState(cwd=cwd)
    try:
        raw = json.loads(state_path(cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict) or raw.get("cwd") != cwd:  # another dir, same encoded name
        return empty
    ids = raw.get("open")
    shown = raw.get("shown")
    return OpenState(
        cwd=cwd,
        all_dirs=raw.get("all_dirs") is True,
        filter=_read_filter(raw.get("filter")),
        open=tuple(i for i in ids if isinstance(i, str) and i) if isinstance(ids, list) else (),
        shown=shown if isinstance(shown, str) and shown else None,
    )


def _names(raw: object) -> tuple[str, ...]:
    return tuple(n for n in raw if isinstance(n, str) and n) if isinstance(raw, list) else ()


def _read_filter(raw: object) -> Filter:
    if not isinstance(raw, dict):
        return NO_FILTER
    return Filter(groups=_names(raw.get("groups")), tags=_names(raw.get("tags")))


def save(state: OpenState) -> None:
    """Write atomically (temp file + rename); missing directories are made 0700.
    Raises OSError.
    """
    path = state_path(state.cwd)
    make_dirs(path.parent, state_home())
    payload = {
        "cwd": state.cwd,
        "all_dirs": state.all_dirs,
        "filter": {"groups": list(state.filter.groups), "tags": list(state.filter.tags)},
        "open": list(state.open),
        "shown": state.shown,
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
