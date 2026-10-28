"""Files of earlier versions, moved once at start to where they live now.

- until 2026-10-18 the labels and the trash lived in Claude's config dir
  (~/.claude/session-switcher-meta.json, ~/.claude/session-switcher-trash/);
- until 2026-10-19 the app was called claude-sessions, so its XDG directories were
  ~/.local/share/claude-sessions/ and ~/.local/state/claude-sessions/.

A sidecar already in the new place wins per session; the old entries it does not
have are added. Each moved trash folder's RESTORE.txt gets its new paths. An old
file that cannot be read is left where it is.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from cabildo.sessionmux import sessions, state, xdgdirs

OLD_NAME = "claude-sessions"


@dataclass(frozen=True)
class OldPlace:
    """Where an earlier version kept the labels and the trash."""

    meta: Path
    trash: Path
    home: Path | None  # the old app directory, removed once empty (None: not ours)


def old_places() -> list[OldPlace]:
    claude = sessions.config_dir()
    old = xdgdirs.data_home() / OLD_NAME
    return [
        OldPlace(
            meta=claude / "session-switcher-meta.json",
            trash=claude / "session-switcher-trash",
            home=None,
        ),
        OldPlace(meta=old / "meta.json", trash=old / "trash", home=old),
    ]


def move_old_files() -> list[str]:
    """Move whatever an earlier version left; one line per thing moved."""
    done: list[str] = []
    for place in old_places():
        done += _move_meta(place.meta)
        done += _move_trash(place.trash)
        if place.home is not None:
            _remove_if_empty(place.home)
    done += _move_open_state()
    return done


def _move_meta(old: Path) -> list[str]:
    done: list[str] = []
    if old.exists():
        with sessions.meta_locked():
            if old.exists():  # another app may have moved it meanwhile
                try:
                    json.loads(old.read_text(encoding="utf-8"))
                except ValueError:  # unreadable: leave it for a person to look at
                    return [f"not moved (not JSON): {old}"]
                entries = sessions.read_meta(old)
                if entries:
                    sessions.save_meta(entries | sessions.load_meta())
                    done.append(f"labels, groups and tags -> {sessions.sidecar_path()}")
                old.unlink()
    lock = old.with_name(old.name + ".lock")
    if lock.exists() and lock.stat().st_size == 0:
        lock.unlink()
    return done


def _move_trash(old: Path) -> list[str]:
    if not old.is_dir():
        return []
    trash = sessions.trash_dir()
    moved = 0
    for folder in sorted(old.iterdir()):
        dest = trash / folder.name
        n = 1
        while dest.exists():
            dest = trash / f"{folder.name}-{n}"
            n += 1
        xdgdirs.make_dirs(trash, xdgdirs.data_home())
        shutil.move(folder, dest)
        restore = dest / "RESTORE.txt"
        if restore.is_file():
            text = restore.read_text(encoding="utf-8")
            restore.write_text(text.replace(f"{folder}/", f"{dest}/"), encoding="utf-8")
        moved += 1
    old.rmdir()
    return [f"trash ({moved} conversation{'s' if moved != 1 else ''}) -> {trash}"] if moved else []


def _move_open_state() -> list[str]:
    """The open conversations per list directory; a file already in the new place wins."""
    home = xdgdirs.state_home() / OLD_NAME
    old = home / "open"
    if not old.is_dir():
        return []
    new = state.open_dir()
    moved = 0
    for f in sorted(old.glob("*.json")):
        if (new / f.name).exists():
            f.unlink()
        else:
            xdgdirs.make_dirs(new, xdgdirs.state_home())
            shutil.move(f, new / f.name)
            moved += 1
    _remove_if_empty(old)
    _remove_if_empty(home)
    return (
        [f"open conversations ({moved} director{'ies' if moved != 1 else 'y'}) -> {new}"]
        if moved
        else []
    )


def _remove_if_empty(d: Path) -> None:
    with contextlib.suppress(OSError):  # missing, or something else is in it: keep
        d.rmdir()
