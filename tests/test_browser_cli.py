"""`cabildo browser` as one command: doctor, status and reap through the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cabildo import cli, procs

SERVER = "node /x/@playwright/mcp/cli.js --isolated"


def write_config(home: Path, args: list[str]) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"cabildo-browser": {"command": "pw", "args": args}}})
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_doctor_exits_0_for_a_good_entry(home: Path, capsys) -> None:
    state = home / "state.json"
    state.write_text('{"cookies": [{}], "origins": []}')
    state.chmod(0o600)
    write_config(home, ["--isolated", "--storage-state", str(state)])
    assert cli.main(["browser", "doctor"]) == 0
    assert "ok    isolated" in capsys.readouterr().out


def test_doctor_exits_1_when_the_entry_turns_headless_on(home: Path, capsys) -> None:
    write_config(home, ["--isolated", "--headless"])
    assert cli.main(["browser", "doctor"]) == 1
    assert "FAIL  headed" in capsys.readouterr().out


def test_status_lists_the_servers_found(home: Path, monkeypatch, capsys) -> None:
    write_config(home, ["--isolated"])
    table = [
        procs.Proc(pid=1, ppid=0, comm="systemd", rss_kb=1, started_at=0.0, cmdline="systemd"),
        procs.Proc(pid=2, ppid=1, comm="claude", rss_kb=1, started_at=0.0, cmdline="claude"),
        procs.Proc(pid=3, ppid=2, comm="node", rss_kb=2048, started_at=0.0, cmdline=SERVER),
    ]
    monkeypatch.setattr(procs, "processes", lambda *_a, **_k: table)
    monkeypatch.setattr("cabildo.browser.fleet._owned", lambda _pid: True)
    assert cli.main(["browser", "status"]) == 0
    out = capsys.readouterr().out
    assert "1 server(s)" in out
    assert "session alive" in out


def test_reap_dry_run_names_what_it_would_stop_and_stops_nothing(
    home: Path, monkeypatch, capsys
) -> None:
    write_config(home, ["--isolated"])
    table = [
        procs.Proc(pid=1, ppid=0, comm="systemd", rss_kb=1, started_at=0.0, cmdline="systemd"),
        procs.Proc(pid=3, ppid=1, comm="node", rss_kb=2048, started_at=0.0, cmdline=SERVER),
    ]
    monkeypatch.setattr(procs, "processes", lambda *_a, **_k: table)
    monkeypatch.setattr("cabildo.browser.fleet._owned", lambda _pid: True)
    monkeypatch.setattr(
        "cabildo.browser.fleet.os.kill", lambda *_a: pytest.fail("a dry run must not signal")
    )
    assert cli.main(["browser", "reap", "--dry-run"]) == 0
    assert "would stop server 3 (session gone)" in capsys.readouterr().out


def test_a_broken_claude_json_is_a_message_not_a_traceback(home: Path, capsys) -> None:
    (home / ".claude.json").write_text("{nope")
    assert cli.main(["browser", "doctor"]) == 1
    assert "not valid JSON" in capsys.readouterr().err
