"""The workspace's global keys: the ones that work anywhere, also inside Claude.

Two kinds, both set in the ``[keys]`` table of the workspace ``config.toml``:

- The leader (default ``C-]``) and one key after it, like tmux's prefix. ``C-]`` reaches
  the app on Linux and macOS with no terminal setting, and Claude Code does not use it.
  Pressing the leader twice sends one ``C-]`` to Claude.
- Direct chords for ``show-list`` and ``toggle-list`` (``Alt-s``, ``Alt-z``), as before.

```toml
[keys]
leader = ["ctrl+]"]
modifier = "ctrl"          # direct chords ctrl+s and ctrl+z instead of the default
show-list = ["alt+s", "ß"] # direct chords for one action

[keys.after-leader]
new = "c"                  # leader, then c: new conversation
```

The app applies the direct chords and the leader through Textual's keymap
(``App.set_keymap``), by binding id (#45).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

MACOS = sys.platform == "darwin"


@dataclass(frozen=True)
class Action:
    """One action the leader can start. ``id`` is its name in ``config.toml``."""

    id: str
    method: str  # the app's ``action_<method>``
    label: str  # what the hint bar and the ``?`` screen write
    key: str  # the default key after the leader (a Textual key name)


ACTIONS: tuple[Action, ...] = (
    Action("show-list", "show_list", "list", "s"),
    Action("toggle-list", "toggle_list", "hide/show list", "z"),
    Action("new", "new", "new", "n"),
    Action("restart", "restart", "restart", "r"),
    Action("palette", "palette", "commands", "p"),
    Action("keys", "toggle_keys", "all keys", "question_mark"),
    Action("quit", "quit", "quit", "q"),
)
BY_ID = {a.id: a for a in ACTIONS}
DIRECT = ("show-list", "toggle-list")  # the actions that also take direct chords
LEADER = "ctrl+right_square_bracket"


@dataclass(frozen=True)
class Keymap:
    """The leader keys, the key after the leader per action, the direct chords per action."""

    leader: tuple[str, ...] = (LEADER,)
    after_leader: dict[str, str] = field(default_factory=lambda: {a.id: a.key for a in ACTIONS})
    direct: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def keymap(self) -> dict[str, str]:
        """The keymap for ``App.set_keymap``: binding id to comma-separated keys."""
        out = {"workspace.leader": ",".join(self.leader)}
        for action in DIRECT:
            keys = self.direct.get(action, ())
            # an action with no chord gets a key no keyboard sends, so its binding stays valid
            out[f"workspace.{action.replace('-', '_')}"] = ",".join(keys) or "f24"
        return out

    def action_for(self, key: str) -> Action | None:
        """The action the key after the leader starts, or None."""
        return next((BY_ID[i] for i, k in self.after_leader.items() if k == key), None)

    def hint(self) -> str:
        """The hint bar shown after the leader: ``s list  z hide/show list  ...``."""
        return "  ".join(
            f"{display(self.after_leader[a.id])} {a.label}"
            for a in ACTIONS
            if a.id in self.after_leader
        )

    def help(self, action: str) -> str:
        """The keys of ``action`` as the ``?`` screen writes them: ``C-] s / Alt-s``."""
        lead = display(self.leader[0])
        keys = (
            [f"{lead} {display(self.after_leader[action])}"] if action in self.after_leader else []
        )
        keys += [display(k) for k in self.direct.get(action, ())]
        return " / ".join(keys)


# On macOS Command never reaches a terminal program (the terminal keeps Cmd-s, Cmd-z for its
# menus), and Option sends Meta only when the terminal is set to. Without that setting
# Option-s types ``ß`` and Option-z ``Ω`` (US layout), so those characters are the keys.
# On Linux they are text and stay unbound.
LINUX_DIRECT = {"show-list": ("alt+s",), "toggle-list": ("alt+z",)}
MAC_DIRECT = {"show-list": ("alt+s", "ß"), "toggle-list": ("alt+z", "Ω")}


def default(*, macos: bool = MACOS) -> Keymap:
    """The keys for this platform when the config sets none."""
    return Keymap(direct=dict(MAC_DIRECT if macos else LINUX_DIRECT))


MODIFIERS = ("alt", "ctrl", "super", "meta", "hyper")


def with_modifier(modifier: str) -> dict[str, tuple[str, ...]]:
    """Direct chords ``modifier+s`` and ``modifier+z``, with no other key."""
    return {"show-list": (f"{modifier}+s",), "toggle-list": (f"{modifier}+z",)}


# what a person writes in config.toml, and the Textual key name for it
_ALIASES = {
    "]": "right_square_bracket",
    "\\": "backslash",
    "?": "question_mark",
    "^": "circumflex_accent",
}
_NAMES = {v: k for k, v in _ALIASES.items()}
_MODS = {"alt": "Alt", "ctrl": "C", "shift": "S", "super": "Cmd", "meta": "M", "hyper": "H"}


def normalize(key: str) -> str:
    """``ctrl+]`` as Textual's ``ctrl+right_square_bracket``; other names unchanged."""
    *mods, base = key.split("+") if key != "+" else ["+"]
    return "+".join([*mods, _ALIASES.get(base, base)])


def display(key: str) -> str:
    """``alt+s`` as ``Alt-s``, ``ctrl+right_square_bracket`` as ``C-]``; a character as itself."""
    *mods, base = key.split("+")
    return "-".join([*(_MODS.get(m, m) for m in mods), _NAMES.get(base, base)])


def valid(key: str) -> bool:
    """A key name Textual can bind: not empty, no comma or space, no empty part."""
    return bool(key) and "," not in key and " " not in key and all(key.split("+"))
