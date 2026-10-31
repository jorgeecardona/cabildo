"""The XDG base directories this app writes to (specifications.freedesktop.org/basedir).

- data  ($XDG_DATA_HOME, default ~/.local/share): labels, groups, tags; the trash.
- state ($XDG_STATE_HOME, default ~/.local/state): the open conversations, for the next start.
- config ($XDG_CONFIG_HOME, default ~/.config): how Claude is started (read only).
- runtime ($XDG_RUNTIME_DIR, else the state directory): the `claude agents` snapshot
  that every running workspace shares.

A value that is not an absolute path is invalid and ignored (the spec). Missing
directories are made with mode 0700; existing ones keep their mode.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "workspace"


def _base(var: str, default: str) -> Path:
    env = os.environ.get(var, "")
    return Path(env) if os.path.isabs(env) else Path.home() / default


def data_home() -> Path:
    return _base("XDG_DATA_HOME", ".local/share")


def config_home() -> Path:
    return _base("XDG_CONFIG_HOME", ".config")


def state_home() -> Path:
    return _base("XDG_STATE_HOME", ".local/state")


def runtime_home() -> Path:
    """$XDG_RUNTIME_DIR; the state directory when it is unset or missing (the spec allows
    a fallback).
    """
    env = os.environ.get("XDG_RUNTIME_DIR", "")
    return Path(env) if os.path.isabs(env) and os.path.isdir(env) else state_home()


def make_dirs(path: Path, base: Path) -> None:
    """Make `path` and its missing parents up to and including `base`, each with mode 0700."""
    todo = []
    d = path
    while not d.is_dir():
        todo.append(d)
        if d == base or d.parent == d:
            break
        d = d.parent
    if base in todo and not base.parent.is_dir():
        base.parent.mkdir(parents=True, exist_ok=True)  # above the base: not ours
    for d in reversed(todo):
        d.mkdir(mode=0o700, exist_ok=True)
