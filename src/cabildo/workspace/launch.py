"""How the workspace starts Claude: model, effort, permission mode and other launch flags.

Read from ``$XDG_CONFIG_HOME/workspace/config.toml`` (default ``~/.config``). A ``[launch]``
table sets the default; ``[groups.<name>.launch]`` overrides it for the conversations of one
group. A missing file means plain ``claude``.

```toml
[launch]
model = "opus"
effort = "high"

[launch.env]
DISABLE_TELEMETRY = "1"

[groups.work.launch]
model = "claude-opus-5-5[1m]"
permission-mode = "plan"

[keys]
show-list = ["alt+s", "ß"]
```

``[keys]`` sets the global keys (see ``keys.py``); an action left out keeps its platform
default.

The values apply when Claude starts (new, resume, restart), never to ``claude attach``: an
attached session is already running.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from cabildo.workspace import keys
from cabildo.workspace.xdgdirs import APP, config_home


class ConfigError(Exception):
    """The config file cannot be read or has a key or value the workspace does not know."""


@dataclass(frozen=True)
class Launch:
    """Flags and env for one ``claude`` start. An empty field leaves Claude's own default."""

    model: str = ""  # --model
    effort: str = ""  # --effort
    permission_mode: str = ""  # --permission-mode
    settings: str = ""  # --settings: a file or a JSON string
    append_system_prompt: str = ""  # --append-system-prompt
    mcp_config: tuple[str, ...] = ()  # --mcp-config
    add_dir: tuple[str, ...] = ()  # --add-dir
    args: tuple[str, ...] = ()  # any other flags, as written
    env: tuple[tuple[str, str], ...] = ()  # set in Claude's environment

    def over(self, base: Launch) -> Launch:
        """``base`` with every field this one sets; ``env`` is merged, this one's wins."""
        merged = {f.name: getattr(self, f.name) or getattr(base, f.name) for f in fields(self)}
        merged["env"] = tuple({**dict(base.env), **dict(self.env)}.items())
        return Launch(**merged)

    def flags(self) -> tuple[str, ...]:
        """The command-line flags, in a fixed order."""
        out: list[str] = []
        for flag, value in (
            ("--model", self.model),
            ("--effort", self.effort),
            ("--permission-mode", self.permission_mode),
            ("--settings", self.settings),
            ("--append-system-prompt", self.append_system_prompt),
        ):
            if value:
                out += [flag, value]
        if self.mcp_config:
            out += ["--mcp-config", *self.mcp_config]
        for d in self.add_dir:  # one flag each: a variadic flag would take what follows
            out += ["--add-dir", d]
        return (*out, *self.args)

    def command(self, *claude: str) -> tuple[str, ...]:
        """``claude`` and its subcommand args with the flags after, behind ``env`` when set."""
        cmd = (*claude, *self.flags())
        if self.env:
            return ("env", *(f"{k}={v}" for k, v in self.env), *cmd)
        return cmd


PLAIN = Launch()


@dataclass(frozen=True)
class Config:
    """The launch default and the per-group overrides (group names compare without case)."""

    launch: Launch = PLAIN
    groups: dict[str, Launch] = field(default_factory=dict)
    keys: keys.GlobalKeys = field(default_factory=keys.default)

    def launch_for(self, group: str) -> Launch:
        """The default, with the group's own values over it."""
        own = self.groups.get(group.casefold())
        return own.over(self.launch) if own else self.launch

    def with_model(self, model: str) -> Config:
        """``cabildo workspace --model``: this model for every conversation, groups included."""
        if not model:
            return self
        return replace(
            self,
            launch=replace(self.launch, model=model),
            groups={g: replace(lc, model=model) for g, lc in self.groups.items()},
        )


def config_path() -> Path:
    return config_home() / APP / "config.toml"


def load(path: Path | None = None) -> Config:
    """The config file, or the plain default when it does not exist.

    Raises:
        ConfigError: a TOML error, an unknown key, or a value of the wrong type.
    """
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        _known(data, {"launch", "groups", "keys"}, "")
        groups = _table(data.get("groups", {}), "groups")
        by_group: dict[str, Launch] = {}
        for name, table in groups.items():
            table = _table(table, f"groups.{name}")
            _known(table, {"launch"}, f"groups.{name}.")
            by_group[name.casefold()] = _launch(table.get("launch", {}), f"groups.{name}.launch")
        return Config(
            launch=_launch(data.get("launch", {}), "launch"),
            groups=by_group,
            keys=_keys(data.get("keys", {})),
        )
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


_STRINGS = ("model", "effort", "permission-mode", "settings", "append-system-prompt")
_LISTS = ("mcp-config", "add-dir", "args")
_PATHS = {"settings", "mcp-config", "add-dir"}  # ``~`` expanded: Claude gets no shell


def _launch(raw: object, where: str) -> Launch:
    table = _table(raw, where)
    _known(table, {*_STRINGS, *_LISTS, "env"}, f"{where}.")
    values: dict[str, object] = {}
    for key in _STRINGS:
        if key in table:
            value = table[key]
            if not isinstance(value, str):
                raise ConfigError(f"{where}.{key} is not a string")
            values[key.replace("-", "_")] = os.path.expanduser(value) if key in _PATHS else value
    for key in _LISTS:
        if key in table:
            items = table[key]
            if not (isinstance(items, list) and all(isinstance(i, str) for i in items)):
                raise ConfigError(f"{where}.{key} is not a list of strings")
            if key in _PATHS:
                items = [os.path.expanduser(str(i)) for i in items]
            values[key.replace("-", "_")] = tuple(items)
    env = _table(table.get("env", {}), f"{where}.env")
    if not all(isinstance(v, str) for v in env.values()):
        raise ConfigError(f"{where}.env values must be strings")
    return Launch(**values, env=tuple(env.items()))  # type: ignore[arg-type]


def _keys(raw: object) -> keys.GlobalKeys:
    """``[keys]``: each action set replaces that action's platform default."""
    table = _table(raw, "keys")
    _known(table, {"show-list", "toggle-list"}, "keys.")
    base = keys.default()
    values: dict[str, tuple[str, ...]] = {}
    for name in ("show-list", "toggle-list"):
        if name not in table:
            continue
        items = table[name]
        if not (isinstance(items, list) and items and all(isinstance(i, str) for i in items)):
            raise ConfigError(f"keys.{name} is not a non-empty list of strings")
        bad = [i for i in items if not keys.valid(i)]
        if bad:
            raise ConfigError(f"keys.{name}: {bad[0]!r} is not a key name")
        values[name.replace("-", "_")] = tuple(items)
    return replace(base, **values)


def _table(raw: object, where: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} is not a table")
    return raw


def _known(table: dict[str, object], keys: set[str], prefix: str) -> None:
    unknown = sorted(set(table) - keys)
    if unknown:
        raise ConfigError(f"unknown key {prefix}{unknown[0]}")
