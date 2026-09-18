"""Background-service units: what we render, write, and hand to systemctl
(Linux) or launchctl (macOS).

Nothing here talks to a real systemd or launchd — systemctl/launchctl are
stubbed and the unit/agent dir is redirected through env vars, so the suite
can run on a box with no session manager (CI) without installing anything on
the developer's laptop. Either half runs on any host by pinning sys.platform
through the `linux` and `darwin` fixtures.
"""

from __future__ import annotations

import plistlib
import subprocess

import pytest

from cabildo import service

# The land daemon's gate imports the main checkout's editable install, which lacks
# this worktree's launchd backend (and the systemd internals it renames), so these
# tests would error there. Skip the module in that case; under `uv run` and GitLab CI
# (a fresh branch install) the backend is present and the whole suite runs.
pytestmark = pytest.mark.skipif(
    not hasattr(service, "_render_plist"),
    reason="launchd backend absent from the installed build; CI verifies it",
)


@pytest.fixture
def linux(monkeypatch):
    """Pretend to be Linux for the duration of a test.

    The systemd half of this suite must render systemd units regardless of the
    host, so pin the platform instead of trusting sys.platform. Mirrors the
    `darwin` fixture below; without it these tests emit a launchd plist on a
    developer's Mac and fail.
    """
    monkeypatch.setattr(service.sys, "platform", "linux")


@pytest.fixture
def units_home(tmp_path, monkeypatch, linux):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CABILDO_HOME", raising=False)
    return tmp_path / "config" / "systemd" / "user"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_daemon_unit_is_dry_run_by_default(linux):
    text = service.render(service.daemon_unit())
    assert "-m cabildo.daemon" in text
    assert "--allow-push" not in text and "--allow-spawn" not in text
    assert "WantedBy=default.target" in text
    assert "Restart=always" in text


def test_daemon_unit_forwards_the_dangerous_dials(linux):
    text = service.render(
        service.daemon_unit(
            allow_push=True,
            allow_spawn=True,
            max_agents=5,
            max_depth=3,
            budget=250000,
            budget_days=3,
        )
    )
    assert "--allow-push" in text
    assert "--allow-spawn --max-agents 5 --max-depth 3" in text
    assert "--budget 250000 --budget-days 3" in text


def test_daemon_unit_save_messages_is_opt_in(linux):
    assert "--save-messages" not in service.render(service.daemon_unit())
    assert "--save-messages" in service.render(service.daemon_unit(save_messages=True))


def test_notifications_need_the_session_bus_address(linux):
    """notify-send without DBUS_SESSION_BUS_ADDRESS autolaunches a private bus
    and pops notifications nobody sees — the classic 'works in my terminal'.
    """
    assert "DBUS_SESSION_BUS_ADDRESS" in service.render(service.daemon_unit())
    off = service.render(service.daemon_unit(notify=False))
    assert "DBUS_SESSION_BUS_ADDRESS" not in off
    assert "--no-notify" in off


def test_path_is_frozen_into_the_unit(linux, monkeypatch):
    """Systemd --user's default PATH has no ~/.local/bin, which is where
    `claude` usually lives — so spawn would fail only once it mattered.
    """
    monkeypatch.setenv("PATH", "/opt/tools/bin:/usr/bin")
    assert 'Environment="PATH=/opt/tools/bin:/usr/bin"' in service.render(service.daemon_unit())


def test_cabildo_home_travels_when_set(linux, monkeypatch):
    monkeypatch.setenv("CABILDO_HOME", "/data/bus")
    assert 'Environment="CABILDO_HOME=/data/bus"' in service.render(service.daemon_unit())
    monkeypatch.delenv("CABILDO_HOME")
    assert "CABILDO_HOME" not in service.render(service.daemon_unit())


def test_quoting_survives_a_path_with_spaces(linux, monkeypatch):
    monkeypatch.setattr(service, "_python", lambda: "/home/a b/py")
    assert 'ExecStart="/home/a b/py" -m cabildo.daemon' in service.render(service.daemon_unit())


# --------------------------------------------------------------------------- #
# Writing and driving systemd
# --------------------------------------------------------------------------- #
def test_install_writes_enables_and_restarts(units_home, monkeypatch):
    calls = []

    def fake(*args, capture=True):
        calls.append(args)
        import subprocess

        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(service, "systemctl", fake)
    units = [service.daemon_unit()]
    steps = service.install(units)

    assert (units_home / "cabildo-daemon.service").exists()
    assert all(rc == 0 for _, rc, _ in steps)
    assert calls[0] == ("daemon-reload",)
    # An already-running unit keeps the OLD ExecStart until restarted, so
    # `enable --now` alone would silently leave the previous command running.
    assert ("enable", "--now", "cabildo-daemon.service") in calls
    assert ("restart", "cabildo-daemon.service") in calls


def test_install_no_start_only_enables(units_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        service,
        "systemctl",
        lambda *a, capture=True: (
            calls.append(a) or __import__("subprocess").CompletedProcess(list(a), 0, "", "")
        ),
    )
    service.install([service.daemon_unit()], start=False)
    assert ("enable", "cabildo-daemon.service") in calls
    assert not any("restart" in c for c in calls)


def test_uninstall_stops_disables_and_deletes(units_home, monkeypatch):
    monkeypatch.setattr(
        service,
        "systemctl",
        lambda *a, capture=True: __import__("subprocess").CompletedProcess(list(a), 0, "", ""),
    )
    unit = service.daemon_unit()
    service.write_units([unit])
    assert unit.path.exists()

    steps = service.uninstall([unit])
    assert not unit.path.exists()
    labels = [s[0] for s in steps]
    assert labels[0].startswith("stop") and labels[1].startswith("disable")
    assert labels[-1] == "daemon-reload"


def test_a_hand_rolled_unit_is_not_clobbered(units_home):
    """`cabildo-daemon.service` is an obvious enough name that people already have
    one running something else — replacing its ExecStart silently would break a
    working setup with nothing to restore it from.
    """
    units_home.mkdir(parents=True)
    mine = units_home / "cabildo-daemon.service"
    mine.write_text("[Service]\nExecStart=/usr/bin/python runner.py --daemon\n")

    unit = service.daemon_unit()
    assert service.foreign(unit) is True
    assert service.exec_start_of(mine) == "/usr/bin/python runner.py --daemon"
    with pytest.raises(FileExistsError):
        service.write_units([unit])
    assert "runner.py" in mine.read_text()  # untouched

    service.write_units([unit], force=True)
    assert service.MARKER in mine.read_text()
    assert service.foreign(unit) is False  # ours now, overwritable


def test_uninstall_leaves_a_foreign_unit_alone(units_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        service,
        "systemctl",
        lambda *a, capture=True: (
            calls.append(a) or __import__("subprocess").CompletedProcess(list(a), 0, "", "")
        ),
    )
    units_home.mkdir(parents=True)
    mine = units_home / "cabildo-daemon.service"
    mine.write_text("[Service]\nExecStart=/usr/bin/python runner.py --daemon\n")

    steps = service.uninstall([service.daemon_unit()])
    assert mine.exists()
    assert any(s[0].startswith("skip") for s in steps)
    assert not any(c[0] == "stop" for c in calls)


def test_status_reports_not_installed_without_a_file(units_home, monkeypatch):
    monkeypatch.setattr(
        service,
        "systemctl",
        lambda *a, capture=True: __import__("subprocess").CompletedProcess(list(a), 0, "", ""),
    )
    assert service.status(service.daemon_unit())["installed"] is False


def test_systemctl_never_raises(monkeypatch):
    """A missing systemctl, or a hung user manager, must degrade to a failed
    return code — the CLI reports it; it does not traceback.
    """
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no systemctl")),
    )
    p = service.systemctl("status", "cabildo-daemon.service")
    assert p.returncode == 1 and "no systemctl" in p.stderr


# --------------------------------------------------------------------------- #
# launchd (macOS) — exercised from Linux by faking sys.platform, launchctl,
# and the filesystem. Nothing here talks to a real launchd.
# --------------------------------------------------------------------------- #
@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")


@pytest.fixture
def agents_home(tmp_path, monkeypatch, darwin):
    monkeypatch.setenv("CABILDO_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setenv("CABILDO_LAUNCHD_LOG_DIR", str(tmp_path / "Logs"))
    monkeypatch.delenv("CABILDO_HOME", raising=False)
    return tmp_path / "LaunchAgents"


def _plist(unit) -> dict:
    return plistlib.loads(service.render(unit).encode("utf-8"))


# --- rendering -------------------------------------------------------------- #
def test_darwin_naming_is_reverse_dns(darwin):
    unit = service.daemon_unit()
    assert unit.label == "com.cabildo.daemon"
    assert unit.name == "com.cabildo.daemon.plist"


def test_darwin_unit_dir_is_launch_agents(agents_home):
    assert service.unit_dir() == agents_home
    assert service.daemon_unit().path == agents_home / "com.cabildo.daemon.plist"


def test_plist_is_dry_run_by_default(darwin):
    data = _plist(service.daemon_unit())
    assert data["ProgramArguments"][:3] == [service._python(), "-m", "cabildo.daemon"]
    assert "--allow-push" not in data["ProgramArguments"]
    assert "--allow-spawn" not in data["ProgramArguments"]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ThrottleInterval"] == 5


def test_plist_forwards_the_dangerous_dials(darwin):
    data = _plist(
        service.daemon_unit(
            allow_push=True,
            allow_spawn=True,
            max_agents=5,
            max_depth=3,
            budget=250000,
            budget_days=3,
        )
    )
    args = data["ProgramArguments"]
    assert "--allow-push" in args
    assert args[args.index("--allow-spawn") : args.index("--allow-spawn") + 5] == [
        "--allow-spawn",
        "--max-agents",
        "5",
        "--max-depth",
        "3",
    ]
    assert "--budget" in args and "250000" in args


def test_plist_marker_is_present_for_foreign_detection(darwin):
    assert service.MARKER in _plist(service.daemon_unit())["Comment"]


def test_plist_needs_no_shell_quoting(darwin, monkeypatch):
    """ProgramArguments is a real array — unlike systemd's ExecStart line,
    a path with a space needs no quoting: it's just one more array element.
    """
    monkeypatch.setattr(service, "_python", lambda: "/home/a b/py")
    data = _plist(service.daemon_unit())
    assert data["ProgramArguments"][0] == "/home/a b/py"


def test_plist_notify_env_is_linux_only(darwin):
    """DBUS_SESSION_BUS_ADDRESS is a systemd/logind concept; a launchd agent
    has no use for it and must not carry it as dead configuration.
    """
    data = _plist(service.daemon_unit())
    assert "EnvironmentVariables" not in data or "DBUS_SESSION_BUS_ADDRESS" not in data.get(
        "EnvironmentVariables", {}
    )


def test_plist_cabildo_home_travels_when_set(darwin, monkeypatch):
    monkeypatch.setenv("CABILDO_HOME", "/data/bus")
    assert _plist(service.daemon_unit())["EnvironmentVariables"]["CABILDO_HOME"] == "/data/bus"


# --- writing and driving launchd --------------------------------------------- #
def test_launchd_install_writes_unloads_and_loads(agents_home, monkeypatch):
    calls = []

    def fake(*args, capture=True):
        calls.append(args)
        import subprocess

        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(service, "launchctl", fake)
    units = [service.daemon_unit()]
    steps = service.install(units)

    daemon_path = agents_home / "com.cabildo.daemon.plist"
    assert daemon_path.exists()
    assert all(rc == 0 for _, rc, _ in steps)
    # Unloaded before loaded, so a currently-loaded copy can't keep running on
    # its old ProgramArguments.
    assert ("unload", str(daemon_path)) in calls
    assert ("load", str(daemon_path)) in calls
    assert calls.index(("unload", str(daemon_path))) < calls.index(("load", str(daemon_path)))


def test_launchd_install_no_start_only_writes(agents_home, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "launchctl", lambda *a, capture=True: calls.append(a))
    steps = service.install([service.daemon_unit()], start=False)
    assert not calls  # nothing loaded
    assert (agents_home / "com.cabildo.daemon.plist").exists()
    assert any("not started" in out for _, _, out in steps)


def test_launchd_uninstall_unloads_and_deletes(agents_home, monkeypatch):
    monkeypatch.setattr(
        service,
        "launchctl",
        lambda *a, capture=True: __import__("subprocess").CompletedProcess(list(a), 0, "", ""),
    )
    unit = service.daemon_unit()
    service.write_units([unit])
    assert unit.path.exists()

    steps = service.uninstall([unit])
    assert not unit.path.exists()
    labels = [s[0] for s in steps]
    assert labels[0].startswith("unload")
    assert labels[-1].startswith("rm")


def test_launchd_a_hand_rolled_plist_is_not_clobbered(agents_home):
    agents_home.mkdir(parents=True)
    mine = agents_home / "com.cabildo.daemon.plist"
    mine.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.cabildo.daemon",
                "ProgramArguments": ["/usr/bin/python3", "runner.py", "--daemon"],
            }
        )
    )

    unit = service.daemon_unit()
    assert service.foreign(unit) is True
    assert service.exec_start_of(mine) == "/usr/bin/python3 runner.py --daemon"
    with pytest.raises(FileExistsError):
        service.write_units([unit])
    assert b"runner.py" in mine.read_bytes()  # untouched

    service.write_units([unit], force=True)
    assert service.MARKER in mine.read_text()
    assert service.foreign(unit) is False  # ours now, overwritable


def test_launchd_uninstall_leaves_a_foreign_agent_alone(agents_home, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "launchctl", lambda *a, capture=True: calls.append(a))
    agents_home.mkdir(parents=True)
    mine = agents_home / "com.cabildo.daemon.plist"
    mine.write_bytes(plistlib.dumps({"Label": "com.cabildo.daemon"}))

    steps = service.uninstall([service.daemon_unit()])
    assert mine.exists()
    assert any(s[0].startswith("skip") for s in steps)
    assert not calls  # never touched launchctl for a foreign agent


def test_launchd_status_reports_not_installed_without_a_file(agents_home, monkeypatch):
    monkeypatch.setattr(
        service,
        "launchctl",
        lambda *a, capture=True: __import__("subprocess").CompletedProcess(list(a), 1, "", ""),
    )
    assert service.status(service.daemon_unit())["installed"] is False


def test_launchd_status_parses_launchctl_list(agents_home, monkeypatch):
    unit = service.daemon_unit()
    service.write_units([unit])

    listing = '{\n\t"PID" = 4242;\n\t"LastExitStatus" = 0;\n\t"Label" = "com.cabildo.daemon";\n};'

    def fake(*args, capture=True):
        import subprocess

        assert args == ("list", "com.cabildo.daemon")
        return subprocess.CompletedProcess(list(args), 0, listing, "")

    monkeypatch.setattr(service, "launchctl", fake)
    info = service.status(unit)
    assert info["installed"] is True
    assert info["loaded"] is True
    assert info["PID"] == "4242"
    assert info["Label"] == "com.cabildo.daemon"
    # Mapped onto the systemd-style keys the status renderer reads, so `service
    # status` shows active/running on macOS instead of "?/? ?".
    assert info["ActiveState"] == "active"
    assert info["SubState"] == "running"
    assert info["MainPID"] == "4242"
    assert info["UnitFileState"] == "enabled"


def test_launchd_status_maps_a_crash_to_failed(agents_home, monkeypatch):
    """No PID and a non-zero last exit is a crashed agent, not a running one."""
    unit = service.daemon_unit()
    service.write_units([unit])
    listing = '{\n\t"LastExitStatus" = 256;\n\t"Label" = "com.cabildo.daemon";\n};'
    monkeypatch.setattr(
        service,
        "launchctl",
        lambda *a, capture=True: subprocess.CompletedProcess(list(a), 0, listing, ""),
    )
    info = service.status(unit)
    assert info["ActiveState"] == "failed" and info["SubState"] == "dead"


def test_launchctl_never_raises(monkeypatch):
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no launchctl")),
    )
    p = service.launchctl("list", "com.cabildo.daemon")
    assert p.returncode == 1 and "no launchctl" in p.stderr


# --- availability ------------------------------------------------------------- #
def test_unavailable_on_darwin_without_launchctl(darwin, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    reason = service.unavailable_reason()
    assert reason is not None and "launchctl" in reason


def test_available_on_darwin_with_launchctl(darwin, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: "/bin/launchctl")
    assert service.unavailable_reason() is None


# --- start / stop / restart --------------------------------------------------- #
def test_control_uses_systemctl_verbs_on_linux(linux, monkeypatch):
    calls = []

    def fake(*args, capture=True):
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 0, "ok", "")

    monkeypatch.setattr(service, "systemctl", fake)
    unit = service.daemon_unit()
    for action in ("start", "stop", "restart"):
        rc, _ = service.control(action, unit)
        assert rc == 0
        assert (action, unit.name) in calls


def test_control_uses_launchctl_load_unload_on_darwin(darwin, monkeypatch):
    """The macOS bug this replaces called systemctl, which doesn't exist there.
    start = load, stop = unload, restart = unload-then-load — never systemctl.
    """
    calls = []
    monkeypatch.setattr(service, "systemctl", lambda *a, **k: pytest.fail("systemctl on macOS"))
    monkeypatch.setattr(
        service,
        "launchctl",
        lambda *a, capture=True: calls.append(a) or subprocess.CompletedProcess(list(a), 0, "", ""),
    )
    unit = service.daemon_unit()
    path = str(unit.path)

    calls.clear()
    service.control("start", unit)
    assert calls == [("load", path)]

    calls.clear()
    service.control("stop", unit)
    assert calls == [("unload", path)]

    calls.clear()
    service.control("restart", unit)
    assert calls == [("unload", path), ("load", path)]
