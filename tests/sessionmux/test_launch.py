"""launch.py: the config file and the claude command it builds."""

from __future__ import annotations

from pathlib import Path

import pytest

from cabildo.sessionmux import launch
from cabildo.sessionmux.launch import Config, ConfigError, Launch

CONFIG = """
[launch]
model = "opus"
effort = "high"
add-dir = ["/a", "/b"]
args = ["--verbose"]

[launch.env]
A = "1"
B = "2"

[groups.Work.launch]
model = "claude-opus-5-5[1m]"
permission-mode = "plan"

[groups.Work.launch.env]
B = "work"
"""


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


def write(home: Path, text: str) -> None:
    path = home / "sessionmux" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_no_file_is_plain_claude(config_home) -> None:
    assert launch.load().launch_for("any").command("claude") == ("claude",)


def test_default_command_has_flags_after_the_subcommand_and_env_in_front(config_home) -> None:
    write(config_home, CONFIG)
    cmd = launch.load().launch.command("claude", "--resume", "s1")
    assert cmd == (
        "env", "A=1", "B=2",
        "claude", "--resume", "s1",
        "--model", "opus", "--effort", "high",
        "--add-dir", "/a", "--add-dir", "/b", "--verbose",
    )  # fmt: skip


def test_a_group_overrides_the_default_and_names_compare_without_case(config_home) -> None:
    write(config_home, CONFIG)
    work = launch.load().launch_for("work")
    assert (work.model, work.effort, work.permission_mode) == (
        "claude-opus-5-5[1m]",
        "high",
        "plan",
    )
    assert dict(work.env) == {"A": "1", "B": "work"}
    assert launch.load().launch_for("other").model == "opus"


def test_the_command_line_model_wins_over_every_group(config_home) -> None:
    write(config_home, CONFIG)
    config = launch.load().with_model("sonnet")
    assert {config.launch_for(g).model for g in ("", "work")} == {"sonnet"}


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[launch]\nmodle = 'opus'", "unknown key launch.modle"),
        ("[launch]\nmodel = 4", "launch.model is not a string"),
        ("[launch]\nadd-dir = '/a'", "launch.add-dir is not a list of strings"),
        ("[groups.w.launch.env]\nX = 1", "groups.w.launch.env values must be strings"),
        ("[lunch]", "unknown key lunch"),
        ("[launch", "Expected ']'"),
    ],
)
def test_a_bad_file_names_the_key(config_home, text: str, error: str) -> None:
    write(config_home, text)
    with pytest.raises(ConfigError, match=error.replace("[", r"\[")):
        launch.load()


def test_over_keeps_the_base_where_unset() -> None:
    merged = Launch(effort="low").over(Launch(model="opus", effort="high"))
    assert (merged.model, merged.effort) == ("opus", "low")


def test_empty_config_is_plain() -> None:
    assert Config().launch_for("x") == Launch()


def test_tilde_is_expanded_in_paths_only(config_home, monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/u")
    write(config_home, '[launch]\nsettings = "~/s.json"\nadd-dir = ["~/n"]\nmodel = "~x"\n')
    lc = launch.load().launch
    assert (lc.settings, lc.add_dir, lc.model) == ("/home/u/s.json", ("/home/u/n",), "~x")
