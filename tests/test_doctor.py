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

# The daemon's push gate runs the suite from a git worktree using a shared venv
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
def test_hooks_all_wired(env):
    home, _ = env
    _write_settings(home, HOOK_ARGS)
    c = doctor.check_hooks()
    assert c.status == doctor.OK
    assert f"{len(HOOK_ARGS)}/{len(HOOK_ARGS)}" in c.detail


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
    assert f"2/{len(HOOK_ARGS)}" in c.detail
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
    assert {c.name for c in checks} >= {"hooks", "mcp.bus", "db", "daemon"}


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


def test_daemon_push_check_reports_dry_run_posture(tmp_path, monkeypatch):
    from cabildo import doctor, service

    unit_path = tmp_path / "cabildo-daemon.unit"

    class _U:
        path = unit_path

    monkeypatch.setattr(service, "daemon_unit", _U)

    # No service installed: not applicable, not an error.
    assert doctor.check_daemon_push().status == doctor.NA

    # Installed without --allow-push: a dry-run daemon that looks like it pushes.
    unit_path.write_text("ExecStart=/x -m cabildo.daemon --allow-spawn\n")
    assert doctor.check_daemon_push().status == doctor.WARN

    # Installed with --allow-push: it really pushes.
    unit_path.write_text("ExecStart=/x -m cabildo.daemon --allow-push\n")
    assert doctor.check_daemon_push().status == doctor.OK


# --------------------------------------------------------------------------- #
# Install drift (#42) and paired machines (#43)
# --------------------------------------------------------------------------- #
def _write_daemon_info(*, pid=None, version=None, exe=None):
    import sys

    from cabildo import __version__
    from cabildo.daemon import info_path

    info_path().parent.mkdir(parents=True, exist_ok=True)
    info_path().write_text(
        json.dumps(
            {
                "pid": os.getpid() if pid is None else pid,
                "version": version or __version__,
                "exe": exe or sys.executable,
            }
        )
    )


def test_daemon_build_is_na_without_a_record(env):
    assert doctor.check_daemon_build().status == doctor.NA


def test_daemon_build_ok_when_same_version_and_exe(env):
    _write_daemon_info()
    assert doctor.check_daemon_build().status == doctor.OK


def test_daemon_build_warns_on_an_older_version(env):
    _write_daemon_info(version="0.0.1")
    c = doctor.check_daemon_build()
    assert c.status == doctor.WARN and "0.0.1" in c.detail and "restart" in c.fix


def test_daemon_build_warns_when_it_runs_from_another_install(env):
    _write_daemon_info(exe="/elsewhere/.venv/bin/python")
    c = doctor.check_daemon_build()
    assert c.status == doctor.WARN and "/elsewhere" in c.detail


def test_daemon_build_na_when_the_recorded_process_is_gone(env):
    _write_daemon_info(pid=2**22 + 12345)
    assert doctor.check_daemon_build().status == doctor.NA


def _proc(cmdline, started_at):
    from cabildo.procs import Proc

    return Proc(pid=4242, ppid=1, comm="python", rss_kb=1, started_at=started_at, cmdline=cmdline)


# check_mcp_fresh reads /proc to find running MCP servers, so it is NA off Linux.
_needs_proc = pytest.mark.skipif(not os.path.isdir("/proc"), reason="check_mcp_fresh needs /proc")


@_needs_proc
def test_mcp_fresh_warns_for_a_server_older_than_the_package(env, monkeypatch):
    import cabildo

    built = os.stat(cabildo.__file__).st_mtime
    monkeypatch.setattr(
        doctor.procs,
        "processes",
        lambda: [_proc("/x/bin/python /x/bin/cabildo-mcp", built - 100)],
    )
    c = doctor.check_mcp_fresh()
    assert c.status == doctor.WARN and "4242" in c.detail


@_needs_proc
def test_mcp_fresh_ok_for_a_newer_server_and_ignores_other_processes(env, monkeypatch):
    import cabildo

    built = os.stat(cabildo.__file__).st_mtime
    monkeypatch.setattr(
        doctor.procs,
        "processes",
        lambda: [
            _proc("/x/bin/python /x/bin/cabildo-mcp", built + 100),
            _proc("vim cabildo-mcp.md", built - 100),
        ],
    )
    assert doctor.check_mcp_fresh().status == doctor.OK


def test_installs_warns_when_two_cabildo_are_on_path(tmp_path, monkeypatch):
    dirs = []
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        exe = d / "cabildo"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        dirs.append(str(d))
    monkeypatch.setenv("PATH", os.pathsep.join(dirs))
    c = doctor.check_installs()
    assert c.status == doctor.WARN and all(d in c.detail for d in dirs)
    monkeypatch.setenv("PATH", dirs[0])
    assert doctor.check_installs().status == doctor.OK


def test_lan_reports_each_paired_machine_and_its_last_pull(env):
    import time

    from cabildo import lan, pair

    assert doctor.check_lan()[0].status == doctor.NA
    pair.save_peer(pair.PairedPeer("n1" * 16, "10.0.0.1", 1111, "pem", 0))
    pair.save_peer(pair.PairedPeer("n2" * 16, "10.0.0.2", 2222, "pem", 0))
    lan.save_status(
        {
            "n1" * 16: lan.SyncStatus(time.time(), pulled=7, merged=0, cursor=50),
            "n2" * 16: lan.SyncStatus(time.time(), 0, 0, 0, "unreachable: timed out"),
        }
    )
    ok, bad, general = doctor.check_lan()
    assert ok.status == doctor.OK and "7 sent, 0 new" in ok.detail
    assert bad.status == doctor.WARN and "unreachable" in bad.detail
    assert general.name == "lan.general"
