"""state.py: where the open-conversations file lives (XDG), its shape, atomic writes."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from cabildo.workspace import state, xdgdirs
from cabildo.workspace.sessions import Filter
from cabildo.workspace.state import OpenState


@pytest.mark.parametrize(
    ("var", "home", "default"),
    [
        ("XDG_STATE_HOME", xdgdirs.state_home, ".local/state"),
        ("XDG_DATA_HOME", xdgdirs.data_home, ".local/share"),
    ],
)
def test_base_dirs_follow_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str, home, default: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(var, "/somewhere/else")
    assert home() == Path("/somewhere/else")
    for invalid in ["", "relative/dir"]:  # the spec: ignore a relative value
        monkeypatch.setenv(var, invalid)
        assert home() == tmp_path / default
    monkeypatch.delenv(var)
    assert home() == tmp_path / default


def test_save_then_load(state_home: Path) -> None:
    saved = OpenState(
        cwd="/work/proj",
        all_dirs=True,
        filter=Filter(groups=("work",), tags=("a", "b"), titles=("trip",)),
        open=("a", "b"),
        shown="b",
    )
    state.save(saved)
    assert state.load("/work/proj") == saved
    assert state.state_path("/work/proj") == (state_home / "workspace" / "open" / "-work-proj.json")


def test_new_directories_are_private_and_existing_ones_keep_their_mode(state_home: Path) -> None:
    state_home.mkdir(mode=0o755)
    os.chmod(state_home, 0o755)
    state.save(OpenState(cwd="/work/proj"))

    def mode(p: Path) -> int:
        return stat.S_IMODE(p.stat().st_mode)

    assert mode(state_home) == 0o755
    assert mode(state_home / "workspace") == mode(state_home / "workspace" / "open") == 0o700


def test_nothing_saved_loads_as_empty() -> None:
    assert state.load("/work/proj") == OpenState(cwd="/work/proj")


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[1, 2]",
        json.dumps({"cwd": "/work/other", "open": ["a"]}),  # another dir with the same name
    ],
)
def test_an_unusable_file_loads_as_empty(content: str) -> None:
    path = state.state_path("/work/proj")
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert state.load("/work/proj") == OpenState(cwd="/work/proj")


def test_wrong_types_are_dropped() -> None:
    path = state.state_path("/work/proj")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "cwd": "/work/proj",
                "all_dirs": "yes",
                "filter": {"groups": "work", "tags": ["a", 1, ""]},
                "open": ["a", 3, ""],
                "shown": 7,
            }
        )
    )
    assert state.load("/work/proj") == OpenState(
        cwd="/work/proj", filter=Filter(tags=("a",)), open=("a",)
    )


def test_a_failed_write_keeps_the_old_file(monkeypatch: pytest.MonkeyPatch) -> None:
    state.save(OpenState(cwd="/work/proj", open=("old",)))

    def crash(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        state.save(OpenState(cwd="/work/proj", open=("new",)))
    assert state.load("/work/proj").open == ("old",)
