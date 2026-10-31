"""Shared fixtures: a throwaway Claude config dir with hand-written transcripts."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from lines import PROJECT

from cabildo.workspace import sessions

# The tests render the app and its terminal pane. Under `moon run` in CI (a non-TTY run) moon
# exports NO_COLOR to its task children, which puts Textual in monochrome mode -- and the
# textual-tty pane's monochrome render path hits a None style and crashes (see docs/issues).
# the workspace is a colour TUI; drop NO_COLOR here so the suite renders the way the app actually
# runs. The real no-colour-terminal robustness fix belongs upstream.
os.environ.pop("NO_COLOR", None)

FAKE_CLAUDE = """#!/bin/sh
# stands in for the claude CLI: print the args and where it runs, set the title like
# Claude does, echo keys
printf 'FAKE %s\\n' "$*"
printf 'CWD %s\\n' "$(pwd)"
printf '\\033]0;fake-%s\\007' "$2"
exec cat
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The app's panes run this script instead of the real `claude`."""
    from cabildo.workspace import app

    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE)
    path.chmod(0o755)
    monkeypatch.setattr(app, "CLAUDE", str(path))
    return path


@pytest.fixture(autouse=True)
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test: CLAUDE_CONFIG_DIR -> an empty tmp dir, never the real ~/.claude
    (the app moves its old files out of there at start).
    """
    home = tmp_path / "claude"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test keeps labels and the trash in tmp, never in ~/.local/share."""
    home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    return home


Transcript = Callable[..., Path]


@pytest.fixture
def transcript(claude_home: Path) -> Transcript:
    """Write `<id>.jsonl` for PROJECT from dict lines; optional mtime (epoch seconds)."""

    def write(session_id: str, *lines: dict, mtime: float | None = None) -> Path:
        pdir = sessions.project_dir(PROJECT)
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps({"sessionId": session_id, **d}) + "\n" for d in lines))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    return write


@pytest.fixture(autouse=True)
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test keeps the app's open-conversations state in tmp, never in ~/.local/state."""
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def runtime_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test shares its `claude agents` snapshot in tmp, never in /run/user."""
    home = tmp_path / "runtime"
    home.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home))
    return home


@pytest.fixture(autouse=True)
def bus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test reads an empty bus in tmp, never the real ~/.cabildo."""
    home = tmp_path / "bus"
    monkeypatch.setenv("CABILDO_HOME", str(home))
    return home
