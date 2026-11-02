"""The two global keys per platform (#45)."""

from __future__ import annotations

from textual.keys import _character_to_key

from cabildo.workspace.app import global_keys


def test_linux_keeps_alt_only() -> None:
    assert global_keys(macos=False) == ("alt+s", "alt+z")


def test_macos_adds_command_and_option_characters() -> None:
    show, toggle = global_keys(macos=True)
    assert show.split(",") == ["super+s", "alt+s", "ß"]
    assert toggle.split(",") == ["super+z", "alt+z", "Ω"]


def test_option_characters_match_textual_key_names() -> None:
    # Textual names a printable key by its character; the binding must use that name.
    assert _character_to_key("ß") == "ß"
    assert _character_to_key("Ω") == "Ω"
