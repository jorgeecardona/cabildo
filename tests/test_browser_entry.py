"""`cabildo browser doctor`: the cabildo-browser entry, the saved login, and the rules."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cabildo.browser import entry
from cabildo.browser.entry import Entry, Finding, StorageState

GOOD_ARGS = ("--browser", "chrome", "--isolated", "--storage-state", "/s/state.json")


def levels(findings: tuple[Finding, ...]) -> dict[str, str]:
    return {f.name: f.level for f in findings}


def make_entry(args: tuple[str, ...] = GOOD_ARGS, env: tuple[tuple[str, str], ...] = ()) -> Entry:
    return Entry(command="playwright-mcp", args=args, env=env)


def fresh_state(tmp_path: Path) -> StorageState:
    return StorageState(path=tmp_path / "s.json", exists=True, cookies=3, origins=1, mode=0o600)


def test_parse_flags_reads_both_flag_forms() -> None:
    flags = entry.parse_flags(
        ["--isolated", "--user-data-dir=/p", "--storage-state", "/s.json", "--config", "/c.json"]
    )
    assert flags.isolated
    assert flags.user_data_dir == "/p"
    assert flags.storage_state == "/s.json"
    assert flags.config_file == "/c.json"
    assert not flags.headless


def test_parse_flags_headless_false_is_not_headless() -> None:
    assert not entry.parse_flags(["--headless=false"]).headless
    assert entry.parse_flags(["--headless"]).headless
    assert entry.parse_flags(["--headless=true"]).headless


def test_a_flag_does_not_swallow_the_next_flag_as_its_value() -> None:
    flags = entry.parse_flags(["--isolated", "--storage-state", "/s.json"])
    assert flags.isolated
    assert flags.storage_state == "/s.json"


def test_load_entry_reads_the_cabildo_browser_server(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cabildo-browser": {"command": "pw", "args": ["--isolated"], "env": {"A": "1"}},
                    "other": {"command": "x"},
                }
            }
        )
    )
    got = entry.load_entry()
    assert got == Entry(command="pw", args=("--isolated",), env=(("A", "1"),))


def test_load_entry_is_none_without_the_file_or_the_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert entry.load_entry() is None
    (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
    assert entry.load_entry() is None


def test_load_entry_rejects_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "c.json"
    bad.write_text("{nope")
    with pytest.raises(ValueError, match="not valid JSON"):
        entry.load_entry(bad)


def test_a_good_entry_passes_every_check(tmp_path: Path) -> None:
    got = entry.check(make_entry(), fresh_state(tmp_path))
    assert all(f.level == "ok" for f in got)


def test_a_missing_entry_fails() -> None:
    assert levels(entry.check(None, None)) == {"entry": "fail"}


def test_missing_isolated_fails() -> None:
    got = entry.check(make_entry(("--browser", "chrome")), None)
    assert levels(got)["isolated"] == "fail"


def test_a_user_data_dir_fails() -> None:
    got = entry.check(make_entry((*GOOD_ARGS, "--user-data-dir", "/p")), None)
    assert levels(got)["user-data-dir"] == "fail"


def test_headless_flag_fails() -> None:
    got = entry.check(make_entry((*GOOD_ARGS, "--headless")), None)
    assert levels(got)["headed"] == "fail"


def test_a_headless_env_var_fails() -> None:
    got = entry.check(make_entry(env=(("PLAYWRIGHT_MCP_HEADLESS", "true"),)), None)
    assert levels(got)["headed"] == "fail"


def test_a_false_headless_env_var_passes() -> None:
    got = entry.check(make_entry(env=(("PLAYWRIGHT_MCP_HEADLESS", "false"),)), None)
    assert levels(got)["headed"] == "ok"


def test_a_config_file_that_turns_headless_on_fails(tmp_path: Path) -> None:
    config = tmp_path / "pw.json"
    config.write_text(json.dumps({"browser": {"launchOptions": {"headless": True}}}))
    got = entry.check(make_entry((*GOOD_ARGS, "--config", str(config))), None)
    assert levels(got)["headed"] == "fail"


def test_storage_state_counts_and_age(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"cookies": [{}, {}], "origins": [{}]}))
    path.chmod(0o600)
    mtime = path.stat().st_mtime
    state = entry.read_storage_state(path, now=mtime + 40 * 86400)
    assert (state.cookies, state.origins, state.mode) == (2, 1, 0o600)
    assert state.stale
    assert not state.empty


def test_an_empty_storage_state_warns(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"cookies": [], "origins": []}')
    state = entry.read_storage_state(path)
    assert state.empty
    assert levels(entry.check(make_entry(), state))["storage-state"] == "warn"


def test_a_missing_storage_state_warns(tmp_path: Path) -> None:
    state = entry.read_storage_state(tmp_path / "none.json")
    assert not state.exists
    assert levels(entry.check(make_entry(), state))["storage-state"] == "warn"


def test_a_world_readable_storage_state_warns(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"cookies": [{}], "origins": []}')
    os.chmod(path, 0o644)
    state = entry.read_storage_state(path)
    assert levels(entry.check(make_entry(), state))["storage-state-mode"] == "warn"


def config_file(tmp_path: Path, browser: dict[str, object]) -> str:
    path = tmp_path / "pw.json"
    path.write_text(json.dumps({"browser": browser}))
    return str(path)


def test_a_config_file_with_a_user_data_dir_fails(tmp_path: Path) -> None:
    config = config_file(tmp_path, {"userDataDir": "/p", "isolated": True})
    got = entry.check(make_entry(("--config", config)), None)
    assert levels(got)["user-data-dir"] == "fail"


def test_a_config_file_that_attaches_to_a_running_browser_fails(tmp_path: Path) -> None:
    config = config_file(tmp_path, {"isolated": True, "cdpEndpoint": "http://localhost:9222"})
    got = entry.check(make_entry(("--config", config)), None)
    assert levels(got)["cdp-endpoint"] == "fail"


def test_isolated_set_by_the_config_file_counts(tmp_path: Path) -> None:
    config = config_file(tmp_path, {"isolated": True})
    got = entry.check(make_entry(("--config", config)), None)
    assert levels(got)["isolated"] == "ok"
