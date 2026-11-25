"""clipboard.py: pick the tool for the session and feed it the text."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from cabildo import clipboard


def fake_tool(folder: Path, name: str, out: Path, exit_code: int = 0) -> None:
    """An executable that writes its stdin to ``out`` and exits with ``exit_code``."""
    script = folder / name
    script.write_text(f"#!/bin/sh\n/bin/cat > {out}\n/bin/echo boom >&2\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Linux X11 session whose PATH holds only the fake tools in ``tmp_path/bin``."""
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return tmp_path


def test_the_tool_for_the_session_gets_the_text(session: Path) -> None:
    out = session / "got"
    fake_tool(session / "bin", "xclip", out)
    assert clipboard.copy("héllo\nworld") == "xclip"
    assert out.read_text() == "héllo\nworld"


def test_candidates_by_platform_and_display(monkeypatch: pytest.MonkeyPatch, session: Path) -> None:
    assert [c[0] for c in clipboard.candidates()] == ["xclip", "xsel"]
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert [c[0] for c in clipboard.candidates()] == ["xclip", "xsel", "wl-copy"]
    monkeypatch.delenv("DISPLAY")
    assert [c[0] for c in clipboard.candidates()] == ["wl-copy"]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert clipboard.candidates() == [["pbcopy"]]


def test_a_failing_tool_falls_through_to_the_next(session: Path) -> None:
    out = session / "got"
    fake_tool(session / "bin", "xclip", session / "ignored", exit_code=1)
    fake_tool(session / "bin", "xsel", out)
    assert clipboard.copy("x") == "xsel"
    assert out.read_text() == "x"


def test_errors_say_what_to_do(session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(clipboard.ClipboardError, match="install xclip or xsel"):
        clipboard.copy("x")
    fake_tool(session / "bin", "xclip", session / "o", exit_code=3)
    with pytest.raises(clipboard.ClipboardError, match="boom"):
        clipboard.copy("x")
    with pytest.raises(clipboard.ClipboardError, match="over the"):
        clipboard.copy("x" * (clipboard.MAX_BYTES + 1))
    monkeypatch.delenv("DISPLAY")
    with pytest.raises(clipboard.ClipboardError, match="no display"):
        clipboard.copy("x")
