"""legacy.py: moving the files of earlier versions to where they live now."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from cabildo.workspace import legacy, sessions, state
from cabildo.workspace.sessions import Meta
from cabildo.workspace.state import OpenState


def claude_place() -> legacy.OldPlace:
    return legacy.old_places()[0]  # ~/.claude/session-switcher-*


def old_name_place() -> legacy.OldPlace:
    return legacy.old_places()[1]  # ~/.local/share/claude-sessions/


def old_trash_folder(trash: Path, name: str, files: list[str]) -> Path:
    folder = trash / name
    folder.mkdir(parents=True)
    lines = []
    for f in files:
        (folder / f).write_text("{}\n")
        lines.append(f"{folder / f} -> /claude/projects/-p/{f}")
    (folder / "RESTORE.txt").write_text(
        "title\nMove each file back to restore the conversation:\n" + "\n".join(lines) + "\n"
    )
    return folder


def test_files_in_claudes_config_dir_move_to_the_data_dir(claude_home, data_home) -> None:
    place = claude_place()
    place.meta.write_text(json.dumps({"s1": {"label": "mine"}}))
    place.meta.with_name(place.meta.name + ".lock").write_text("")
    old_trash_folder(place.trash, "20261018-003806-29e2e9d9", ["29e2e9d9.jsonl"])
    old_trash_folder(place.trash, "20261018-072721-cd10c063", ["cd10c063.jsonl", "201ce8d0.jsonl"])

    done = legacy.move_old_files()

    assert done == [
        f"labels, groups and tags -> {data_home}/workspace/meta.json",
        f"trash (2 conversations) -> {data_home}/workspace/trash",
    ]
    assert sessions.load_meta() == {"s1": Meta(label="mine")}
    assert sorted(p.name for p in claude_home.iterdir()) == []  # nothing left behind
    moved = sessions.trash_dir() / "20261018-072721-cd10c063"
    restore = (moved / "RESTORE.txt").read_text()
    for f in ["cd10c063.jsonl", "201ce8d0.jsonl"]:
        assert (moved / f).exists() and f"{moved / f} -> /claude/projects/-p/{f}" in restore
    assert str(place.trash) not in restore
    assert stat.S_IMODE((data_home / "workspace").stat().st_mode) == 0o700
    assert legacy.move_old_files() == []  # once


def test_the_old_app_name_directories_move(data_home, state_home) -> None:
    place = old_name_place()
    place.meta.parent.mkdir(parents=True)
    place.meta.write_text(json.dumps({"s2": {"group": "g"}}))
    place.meta.with_name(place.meta.name + ".lock").write_text("")
    old_trash_folder(place.trash, "20261019-002551-1aab56da", ["1aab56da.jsonl"])
    old_open = state_home / "claude-sessions" / "open"
    old_open.mkdir(parents=True)
    (old_open / "-work-proj.json").write_text(
        json.dumps({"cwd": "/work/proj", "all_dirs": False, "open": ["a"], "shown": "a"})
    )

    done = legacy.move_old_files()

    assert done == [
        f"labels, groups and tags -> {data_home}/workspace/meta.json",
        f"trash (1 conversation) -> {data_home}/workspace/trash",
        f"open conversations (1 directory) -> {state_home}/workspace/open",
    ]
    assert sessions.load_meta() == {"s2": Meta(group="g")}
    assert state.load("/work/proj") == OpenState(cwd="/work/proj", open=("a",), shown="a")
    moved = sessions.trash_dir() / "20261019-002551-1aab56da"
    assert f"{moved / '1aab56da.jsonl'} -> " in (moved / "RESTORE.txt").read_text()
    assert not (data_home / "claude-sessions").exists()
    assert not (state_home / "claude-sessions").exists()


def test_an_open_state_already_in_the_new_place_wins(state_home) -> None:
    state.save(OpenState(cwd="/work/proj", open=("new",)))
    old_open = state_home / "claude-sessions" / "open"
    old_open.mkdir(parents=True)
    (old_open / "-work-proj.json").write_text(json.dumps({"cwd": "/work/proj", "open": ["old"]}))
    assert legacy.move_old_files() == []
    assert state.load("/work/proj").open == ("new",)
    assert not (state_home / "claude-sessions").exists()


def test_something_unknown_in_the_old_app_dir_keeps_it(data_home) -> None:
    home = data_home / "claude-sessions"
    home.mkdir(parents=True)
    (home / "notes.txt").write_text("mine")
    legacy.move_old_files()
    assert (home / "notes.txt").read_text() == "mine"


def test_a_sidecar_already_there_wins_and_old_entries_are_added() -> None:
    sessions.save_meta({"both": Meta(label="new"), "new-only": Meta(group="g")})
    claude_place().meta.write_text(
        json.dumps({"both": {"label": "old"}, "old-only": {"tags": ["t"]}})
    )
    legacy.move_old_files()
    assert sessions.load_meta() == {
        "both": Meta(label="new"),
        "new-only": Meta(group="g"),
        "old-only": Meta(tags=("t",)),
    }
    assert not claude_place().meta.exists()


def test_an_unreadable_old_sidecar_is_left_in_place() -> None:
    old = claude_place().meta
    old.write_text("{half written")
    assert legacy.move_old_files() == [f"not moved (not JSON): {old}"]
    assert old.read_text() == "{half written"


def test_a_trash_folder_name_already_taken_gets_a_suffix() -> None:
    (sessions.trash_dir() / "20261018-003806-29e2e9d9").mkdir(parents=True)
    old_trash_folder(claude_place().trash, "20261018-003806-29e2e9d9", ["a.jsonl"])
    legacy.move_old_files()
    assert (sessions.trash_dir() / "20261018-003806-29e2e9d9-1" / "a.jsonl").exists()


def test_an_empty_old_sidecar_is_removed_without_a_note() -> None:
    claude_place().meta.write_text("{}")
    assert legacy.move_old_files() == []
    assert not claude_place().meta.exists()


def test_nothing_to_move() -> None:
    assert legacy.move_old_files() == []
