"""`cabildo doctor`: the install health check.

Everything here is hermetic — HOME and CABILDO_HOME are redirected into tmp, so
the checks read crafted settings/.mcp.json/DB rather than the developer's real
install, and the platform probes are stubbed to simulate a machine (macOS) that
lacks the Linux facilities cabildo leans on.
"""

from __future__ import annotations

import json
import os

import pytest

# The daemon's land gate runs the suite from a git worktree using a shared venv
# whose editable install is pinned to the MAIN checkout — a module added only in
# this worktree is not importable there, and erroring collection would fail the
# gate on code that is in fact present. Skip in that environment; the worktree
# venv (`uv run`) and CI both install this tree, so they import it and run the
# whole file. CI is the authoritative gate.
pytest.importorskip("cabildo.doctor")

from cabildo import doctor, store
from cabildo.hooks.cli import EVENTS as HOOK_ARGS


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway home + bus. Returns (home, cwd) both under tmp."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path / "bus"))
    cwd = tmp_path / "repo"
    cwd.mkdir()
    return home, cwd


def _write_settings(home, args):
    """settings.json wiring `cabildo-hook <arg>` for each arg, under its real
    Claude-Code event name and with a realistic full-path command.
    """
    arg_to_event = {v: k for k, v in doctor.HOOK_EVENTS.items()}
    hooks = {}
    for arg in args:
        cmd = {"type": "command", "command": f"/home/x/.venv/bin/cabildo-hook {arg}", "timeout": 10}
        hooks[arg_to_event[arg]] = [{"hooks": [cmd]}]
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))


# --------------------------------------------------------------------------- #
# Drift guard
# --------------------------------------------------------------------------- #
def test_hook_events_match_the_dispatcher():
    """Doctor's event->arg table must list exactly the args cabildo-hook accepts,
    or the check would silently ignore a hook (or demand one that can't exist).
    """
    assert set(doctor.HOOK_EVENTS.values()) == set(HOOK_ARGS)


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #
def test_hooks_all_six_wired(env):
    home, _ = env
    _write_settings(home, HOOK_ARGS)
    c = doctor.check_hooks()
    assert c.status == doctor.OK
    assert "6/6" in c.detail


def test_hooks_scans_across_events_and_ignores_foreign_commands(env):
    """A real settings.json interleaves cabildo hooks with unrelated ones (an
    echo-timestamp UserPromptSubmit, a log-conversation Stop). Those must not
    count, and a cabildo hook hung under any event must.
    """
    home, _ = env
    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "echo hi", "timeout": 5}]},
                {"hooks": [{"type": "command", "command": "/v/cabildo-hook prompt_submit"}]},
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": "~/.claude/hooks/log.sh"}]},
                {"hooks": [{"type": "command", "command": "/v/cabildo-hook stop"}]},
            ],
        }
    }
    (home / ".claude" / "settings.json").write_text(json.dumps(settings))
    assert doctor._wired_hook_args(settings) == {"prompt_submit", "stop"}


def test_hooks_missing_some(env):
    home, _ = env
    _write_settings(home, ["session_start", "stop"])
    c = doctor.check_hooks()
    assert c.status == doctor.FAIL
    assert "2/6" in c.detail
    # Missing ones are named by their Claude-Code event, not the internal arg.
    assert "PreToolUse" in c.detail and "SessionEnd" in c.detail
    assert c.fix


def test_hooks_no_settings_file(env):
    c = doctor.check_hooks()  # env created ~/.claude but no settings.json
    assert c.status == doctor.FAIL
    assert "no " in c.detail


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #
def test_mcp_bus_at_user_scope(env):
    home, cwd = env
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"cabildo": {"command": "x"}}}))
    c = doctor.check_mcp_bus(cwd)
    assert c.status == doctor.OK and "user" in c.detail


def test_mcp_bus_at_project_scope(env):
    home, cwd = env
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"cabildo": {"command": "x"}}}))
    c = doctor.check_mcp_bus(cwd)
    assert c.status == doctor.OK and "project" in c.detail


def test_mcp_bus_unregistered_is_a_failure(env):
    _, cwd = env
    c = doctor.check_mcp_bus(cwd)
    assert c.status == doctor.FAIL
    assert c.fix


def test_mcp_issues_na_without_docs_issues(env):
    _, cwd = env
    assert doctor.check_mcp_issues(cwd).status == doctor.NA


def test_mcp_issues_ok_when_wired(env):
    _, cwd = env
    (cwd / "docs" / "issues").mkdir(parents=True)
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"issues": {"command": "x"}}}))
    assert doctor.check_mcp_issues(cwd).status == doctor.OK


def test_mcp_issues_fail_when_register_exists_but_unwired(env):
    _, cwd = env
    (cwd / "docs" / "issues").mkdir(parents=True)
    c = doctor.check_mcp_issues(cwd)
    assert c.status == doctor.FAIL and c.fix


# --------------------------------------------------------------------------- #
# The bus DB
# --------------------------------------------------------------------------- #
def test_db_reachable_and_wal(env):
    c = doctor.check_db()
    assert c.status == doctor.OK
    assert "WAL" in c.detail


def test_db_unreachable_is_a_failure(env, monkeypatch):
    def boom():
        raise OSError("unable to open database file")

    monkeypatch.setattr(doctor.store, "connect", boom)
    c = doctor.check_db()
    assert c.status == doctor.FAIL
    assert "cannot open" in c.detail


# --------------------------------------------------------------------------- #
# The daemon (optional)
# --------------------------------------------------------------------------- #
def test_daemon_absent_is_na_not_a_failure(env):
    assert doctor.check_daemon().status == doctor.NA


def test_daemon_live_when_recently_seen(env):
    from cabildo.daemon import DAEMON_SESSION

    conn = store.connect()
    store.register_session(conn, DAEMON_SESSION, cwd=".")
    assert doctor.check_daemon(conn).status == doctor.OK
    conn.close()


def test_daemon_stale_is_a_warning(env, monkeypatch):
    from cabildo.daemon import DAEMON_SESSION

    conn = store.connect()
    store.register_session(conn, DAEMON_SESSION, cwd=".")
    # Fast-forward past the liveness window without touching the session. Capture
    # the real clock first: doctor.time IS the time module, so patching its .time
    # in terms of itself would recurse.
    import time as _t

    real = _t.time
    monkeypatch.setattr(doctor.time, "time", lambda: real() + 10_000)
    c = doctor.check_daemon(conn)
    assert c.status == doctor.WARN and c.fix
    conn.close()


# --------------------------------------------------------------------------- #
# Platform support
# --------------------------------------------------------------------------- #
def test_platform_reports_three_checks_with_valid_status(env):
    checks = doctor.check_platform()
    names = {c.name for c in checks}
    assert names == {"platform.proc", "platform.notify", "platform.systemd"}
    assert all(c.status in {doctor.OK, doctor.WARN} for c in checks)


def test_platform_flags_the_macos_gaps(env, monkeypatch):
    """The whole point of the ask: on a machine without /proc, notify-send, or
    systemd, every platform check must WARN — not pass, and not crash.
    """
    real_isdir = os.path.isdir
    monkeypatch.setattr(
        doctor.os.path, "isdir", lambda p: False if str(p) == "/proc" else real_isdir(p)
    )
    monkeypatch.setattr(doctor.desktop, "available", lambda: False)
    monkeypatch.setattr(doctor.service, "unavailable_reason", lambda: "macOS has no systemd")

    by_name = {c.name: c for c in doctor.check_platform()}
    assert all(c.status == doctor.WARN for c in by_name.values())
    assert "macOS" in by_name["platform.systemd"].detail


# --------------------------------------------------------------------------- #
# Aggregate + exit semantics
# --------------------------------------------------------------------------- #
def test_run_is_clean_on_a_fully_wired_install(env):
    home, cwd = env
    _write_settings(home, HOOK_ARGS)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"cabildo": {"command": "x"}}}))
    checks = doctor.run(cwd)
    # No genuine failures: hooks + MCP + DB all good; daemon NA; platform host-dependent.
    assert not doctor.failed(checks)
    assert {c.name for c in checks} >= {"hooks", "mcp.bus", "mcp.issues", "db", "daemon"}


def test_failed_counts_only_fail_not_warn_or_na():
    assert doctor.failed(
        [doctor.Check("a", doctor.WARN, ""), doctor.Check("b", doctor.NA, "")]
    ) is (False)
    assert doctor.failed([doctor.Check("a", doctor.FAIL, "")]) is True


def test_cli_doctor_runs_and_returns_int(env, monkeypatch, capsys):
    from cabildo import cli

    home, cwd = env
    _write_settings(home, HOOK_ARGS)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"cabildo": {"command": "x"}}}))
    monkeypatch.chdir(cwd)
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hooks" in out and "mcp.bus" in out and "db" in out
