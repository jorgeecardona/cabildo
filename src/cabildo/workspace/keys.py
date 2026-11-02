"""The workspace's global keys: the ones that work anywhere, also inside Claude.

Each platform has its own defaults. A ``[keys]`` table in the workspace ``config.toml``
replaces them, one list of Textual key names per action:

```toml
[keys]
modifier = "ctrl"   # ctrl+s and ctrl+z instead of the platform default
show-list = ["alt+s", "ß"]
toggle-list = ["alt+z", "Ω"]
```

The app applies them through Textual's keymap (``App.set_keymap``), by binding id (#45).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

MACOS = sys.platform == "darwin"


@dataclass(frozen=True)
class GlobalKeys:
    """The Textual key names bound to each global action. Every tuple has at least one key."""

    show_list: tuple[str, ...]
    toggle_list: tuple[str, ...]

    def keymap(self) -> dict[str, str]:
        """The keymap for ``App.set_keymap``: binding id to comma-separated keys."""
        return {f"workspace.{action}": ",".join(keys) for action, keys in self.by_action()}

    def by_action(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return (("show_list", self.show_list), ("toggle_list", self.toggle_list))

    def help(self, action: str) -> str:
        """The keys of ``action`` as the ``?`` screen writes them: ``Alt-s / ß``."""
        keys = dict(self.by_action())[action]
        return " / ".join(display(k) for k in keys)


# On macOS Command never reaches a terminal program (the terminal keeps Cmd-s, Cmd-z for its
# menus), and Option sends Meta only when the terminal is set to. Without that setting
# Option-s types ``ß`` and Option-z ``Ω`` (US layout), so those characters are the keys.
# On Linux they are text and stay unbound.
LINUX = GlobalKeys(show_list=("alt+s",), toggle_list=("alt+z",))
MAC = GlobalKeys(show_list=("alt+s", "ß"), toggle_list=("alt+z", "Ω"))


def default(*, macos: bool = MACOS) -> GlobalKeys:
    """The keys for this platform when the config sets none."""
    return MAC if macos else LINUX


MODIFIERS = ("alt", "ctrl", "super", "meta", "hyper")


def with_modifier(modifier: str) -> GlobalKeys:
    """``modifier+s`` and ``modifier+z``, with no other key. ``show-list`` / ``toggle-list``
    set after it still win.
    """
    return GlobalKeys(show_list=(f"{modifier}+s",), toggle_list=(f"{modifier}+z",))


_MODS = {"alt": "Alt", "ctrl": "C", "shift": "S", "super": "Cmd", "meta": "M", "hyper": "H"}


def display(key: str) -> str:
    """``alt+s`` as ``Alt-s``, ``ctrl+shift+x`` as ``C-S-x``; a bare character as itself."""
    *mods, base = key.split("+")
    return "-".join([*(_MODS.get(m, m) for m in mods), base])


def valid(key: str) -> bool:
    """A key name Textual can bind: not empty, no comma or space, no empty part."""
    return bool(key) and "," not in key and " " not in key and all(key.split("+"))
