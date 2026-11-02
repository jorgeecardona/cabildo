"""The global keys: platform defaults, ``[keys]`` in config.toml, the keymap (#45)."""

from __future__ import annotations

from pathlib import Path

import pytest
from lines import ai_title, user
from pilot import drive, settle
from textual.keys import _character_to_key

from cabildo.workspace import keys, launch
from cabildo.workspace.app import SwitcherApp, keys_help


def test_linux_default_is_alt_only() -> None:
    assert keys.default(macos=False) == keys.GlobalKeys(("alt+s",), ("alt+z",))


def test_macos_default_adds_the_option_characters() -> None:
    assert keys.default(macos=True) == keys.GlobalKeys(("alt+s", "ß"), ("alt+z", "Ω"))


def test_option_characters_are_textual_key_names() -> None:
    assert _character_to_key("ß") == "ß"
    assert _character_to_key("Ω") == "Ω"


def test_keymap_targets_the_app_binding_ids() -> None:
    ids = {b.id for b in SwitcherApp.BINDINGS if not isinstance(b, tuple) and b.priority}
    keymap = keys.GlobalKeys(("alt+s", "ß"), ("f12",)).keymap()
    assert keymap == {"workspace.show_list": "alt+s,ß", "workspace.toggle_list": "f12"}
    assert set(keymap) <= ids


def test_config_keys_replace_one_action(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[keys]\ntoggle-list = ["ctrl+backslash"]\n', encoding="utf-8")
    got = launch.load(path).keys
    assert got.toggle_list == ("ctrl+backslash",)
    assert got.show_list == keys.default().show_list


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('[keys]\nshow-list = "alt+s"\n', "not a non-empty list"),
        ("[keys]\nshow-list = []\n", "not a non-empty list"),
        ('[keys]\nshow-list = ["alt+s,ß"]\n', "is not a key name"),
        ('[keys]\nquit = ["q"]\n', "unknown key keys.quit"),
    ],
)
def test_config_keys_errors(tmp_path: Path, body: str, error: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(launch.ConfigError, match=error):
        launch.load(path)


def test_help_shows_the_bound_keys() -> None:
    text = keys_help(keys.GlobalKeys(("alt+s", "ß"), ("super+z",)))
    assert "Alt-s / ß" in text
    assert "Cmd-z" in text


def test_configured_key_hides_the_list_and_the_default_no_longer_does(
    transcript, monkeypatch
) -> None:
    transcript("aaaa1111", user("one"), ai_title("One"))
    config = launch.Config(keys=keys.GlobalKeys(("alt+s",), ("f12",)))

    async def scenario(app, pilot) -> None:
        side = app.query_one("#side")
        await pilot.press("alt+z")
        await settle(app, pilot)
        assert side.display  # alt+z is no longer bound
        await pilot.press("f12")
        await settle(app, pilot)
        assert not side.display

    drive(monkeypatch, scenario, config=config)
