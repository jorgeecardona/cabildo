"""The global keys: the leader, direct chords, ``[keys]`` in config.toml, the keymap (#45)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from lines import ai_title, user
from pilot import drive, settle
from textual.keys import Keys, _character_to_key
from textual.widgets import Static

from cabildo.workspace import keys, launch
from cabildo.workspace.app import LeaderScreen, SwitcherApp, keys_help


def test_linux_default_is_leader_and_alt() -> None:
    got = keys.default(macos=False)
    assert got.leader == ("ctrl+right_square_bracket",)
    assert got.direct == {"show-list": ("alt+s",), "toggle-list": ("alt+z",)}
    assert got.after_leader["show-list"] == "s"


def test_macos_default_adds_the_option_characters() -> None:
    assert keys.default(macos=True).direct == {
        "show-list": ("alt+s", "ß"),
        "toggle-list": ("alt+z", "Ω"),
    }


def test_ctrl_bracket_is_the_textual_key_for_0x1d() -> None:
    assert _character_to_key("ß") == "ß"
    assert Keys.ControlSquareClose.value == keys.LEADER


def test_keymap_targets_the_app_binding_ids() -> None:
    ids = {b.id for b in SwitcherApp.BINDINGS if not isinstance(b, tuple) and b.priority}
    keymap = keys.Keymap(direct={"show-list": ("alt+s", "ß")}).keymap()
    assert keymap == {
        "workspace.leader": "ctrl+right_square_bracket",
        "workspace.show_list": "alt+s,ß",
        "workspace.toggle_list": "f24",
    }
    assert set(keymap) <= ids


def test_every_action_names_an_app_method() -> None:
    for action in keys.ACTIONS:
        assert callable(getattr(SwitcherApp, f"action_{action.method}", None)), action.id


def test_config_keys_replace_one_action(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[keys]\ntoggle-list = ["ctrl+\\\\"]\nleader = ["ctrl+]", "f12"]\n'
        '[keys.after-leader]\nnew = "c"\n',
        encoding="utf-8",
    )
    got = launch.load(path).keys
    assert got.direct["toggle-list"] == ("ctrl+backslash",)
    assert got.direct["show-list"] == keys.default().direct["show-list"]
    assert got.leader == ("ctrl+right_square_bracket", "f12")
    assert got.action_for("c") == keys.BY_ID["new"]
    assert got.action_for("n") is None


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('[keys]\nshow-list = "alt+s"\n', "not a non-empty list"),
        ("[keys]\nshow-list = []\n", "not a non-empty list"),
        ('[keys]\nshow-list = ["alt+s,ß"]\n', "is not a key name"),
        ('[keys]\nquit = ["q"]\n', "unknown key keys.quit"),
        ('[keys.after-leader]\nfly = "f"\n', "unknown key keys.after-leader.fly"),
        ("[keys.after-leader]\nnew = 3\n", "is not a key name"),
        ('[keys.after-leader]\nnew = "s"\n', "set for two actions"),
    ],
)
def test_config_keys_errors(tmp_path: Path, body: str, error: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(launch.ConfigError, match=error):
        launch.load(path)


def test_help_shows_the_leader_and_the_chords() -> None:
    text = keys_help(keys.Keymap(direct={"show-list": ("alt+s", "ß"), "toggle-list": ("super+z",)}))
    assert "C-] s / Alt-s / ß" in text
    assert "C-] z / Cmd-z" in text
    assert "C-] C-]" in text


def test_hint_lists_every_action() -> None:
    hint = keys.Keymap().hint()
    for action in keys.ACTIONS:
        assert action.label in hint


def test_modifier_rebinds_both_and_an_action_still_wins(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[keys]\nmodifier = "ctrl"\ntoggle-list = ["f12"]\n', encoding="utf-8")
    assert launch.load(path).keys.direct == {"show-list": ("ctrl+s",), "toggle-list": ("f12",)}


def test_unknown_modifier_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[keys]\nmodifier = "cmd"\n', encoding="utf-8")
    with pytest.raises(launch.ConfigError, match=r"keys\.modifier"):
        launch.load(path)


def test_configured_key_hides_the_list_and_the_default_no_longer_does(
    transcript, monkeypatch
) -> None:
    transcript("aaaa1111", user("one"), ai_title("One"))
    config = launch.Config(keys=keys.Keymap(direct={"toggle-list": ("f12",)}))

    async def scenario(app, pilot) -> None:
        side = app.query_one("#side")
        await pilot.press("alt+z")
        await settle(app, pilot)
        assert side.display  # alt+z is no longer bound
        await pilot.press("f12")
        await settle(app, pilot)
        assert not side.display

    drive(monkeypatch, scenario, config=config)


def test_leader_then_z_hides_the_list_and_shows_the_hint(transcript, monkeypatch) -> None:
    transcript("aaaa1111", user("one"), ai_title("One"))

    async def scenario(app, pilot) -> None:
        side = app.query_one("#side")
        await pilot.press(keys.LEADER)
        await pilot.pause()
        assert isinstance(app.screen, LeaderScreen)
        assert "hide/show list" in str(app.screen.query_one("#leader-hint", Static).render())
        await pilot.press("z")
        await settle(app, pilot)
        assert not isinstance(app.screen, LeaderScreen)
        assert not side.display

    drive(monkeypatch, scenario)


def test_leader_times_out_and_swallows_nothing_after(transcript, monkeypatch) -> None:
    transcript("aaaa1111", user("one"), ai_title("One"))
    monkeypatch.setattr(LeaderScreen, "TIMEOUT", 0.05)

    async def scenario(app, pilot) -> None:
        await pilot.press(keys.LEADER)
        await pilot.pause(0.3)
        assert not isinstance(app.screen, LeaderScreen)
        await pilot.press("z")  # a plain z in the list: not toggle
        await settle(app, pilot)
        assert app.query_one("#side").display

    drive(monkeypatch, scenario)


def test_unbound_key_after_leader_does_nothing(transcript, monkeypatch) -> None:
    transcript("aaaa1111", user("one"), ai_title("One"))

    async def scenario(app, pilot) -> None:
        await pilot.press(keys.LEADER, "y")
        await settle(app, pilot)
        assert not isinstance(app.screen, LeaderScreen)
        assert app.query_one("#side").display

    drive(monkeypatch, scenario)


def test_leader_twice_sends_the_raw_byte_to_claude(transcript, monkeypatch) -> None:
    transcript("aaaa1111", user("one"), ai_title("One"))
    sent: list[str] = []
    display = SimpleNamespace(input_key=sent.append)
    pane = SimpleNamespace(board=SimpleNamespace(display=display))

    async def scenario(app, pilot) -> None:
        monkeypatch.setattr(app, "_shown_pane", lambda: pane)
        await pilot.press(keys.LEADER, keys.LEADER)
        await settle(app, pilot)
        assert not isinstance(app.screen, LeaderScreen)

    drive(monkeypatch, scenario)
    assert sent == ["\x1d"]
