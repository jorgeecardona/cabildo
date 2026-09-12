"""systemd --user units: what we render, write, and hand to systemctl.

Nothing here talks to a real systemd — systemctl is stubbed and the unit dir is
redirected through XDG_CONFIG_HOME, so the suite can run on a box with no
session manager (CI) without installing anything on the developer's laptop.
"""

from __future__ import annotations

import pytest

from cabildo import service


@pytest.fixture
def units_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CABILDO_HOME", raising=False)
    return tmp_path / "config" / "systemd" / "user"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_daemon_unit_is_dry_run_by_default():
    text = service.render(service.daemon_unit())
    assert "-m cabildo.daemon" in text
    assert "--allow-push" not in text and "--allow-spawn" not in text
    assert "WantedBy=default.target" in text
    assert "Restart=always" in text


def test_daemon_unit_forwards_the_dangerous_dials():
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


def test_notifications_need_the_session_bus_address():
    """notify-send without DBUS_SESSION_BUS_ADDRESS autolaunches a private bus
    and pops notifications nobody sees — the classic 'works in my terminal'.
    """
    assert "DBUS_SESSION_BUS_ADDRESS" in service.render(service.daemon_unit())
    off = service.render(service.daemon_unit(notify=False))
    assert "DBUS_SESSION_BUS_ADDRESS" not in off
    assert "--no-notify" in off


def test_path_is_frozen_into_the_unit(monkeypatch):
    """Systemd --user's default PATH has no ~/.local/bin, which is where
    `claude` usually lives — so spawn would fail only once it mattered.
    """
    monkeypatch.setenv("PATH", "/opt/tools/bin:/usr/bin")
    assert 'Environment="PATH=/opt/tools/bin:/usr/bin"' in service.render(service.daemon_unit())


def test_cabildo_home_travels_when_set(monkeypatch):
    monkeypatch.setenv("CABILDO_HOME", "/data/bus")
    assert 'Environment="CABILDO_HOME=/data/bus"' in service.render(service.daemon_unit())
    monkeypatch.delenv("CABILDO_HOME")
    assert "CABILDO_HOME" not in service.render(service.daemon_unit())


def test_sync_unit_loops_and_keeps_the_token_out_of_the_unit():
    text = service.render(service.sync_unit(interval=30, base="https://example.test"))
    assert "cabildo.cli sync --loop --interval 30" in text
    assert "--base https://example.test" in text
    assert "token" not in text.lower()  # it stays in cloud.json, mode 600
    assert "RestartSec=30" in text  # back off harder than the daemon


def test_quoting_survives_a_path_with_spaces(monkeypatch):
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
    units = [service.daemon_unit(), service.sync_unit()]
    steps = service.install(units)

    assert (units_home / "cabildo-daemon.service").exists()
    assert (units_home / "cabildo-sync.service").exists()
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
    """`cabildo-sync.service` is an obvious enough name that people already have
    one running something else — replacing its ExecStart silently would break a
    working setup with nothing to restore it from.
    """
    units_home.mkdir(parents=True)
    mine = units_home / "cabildo-sync.service"
    mine.write_text("[Service]\nExecStart=/usr/bin/python runner.py --sync\n")

    unit = service.sync_unit()
    assert service.foreign(unit) is True
    assert service.exec_start_of(mine) == "/usr/bin/python runner.py --sync"
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
    mine = units_home / "cabildo-sync.service"
    mine.write_text("[Service]\nExecStart=/usr/bin/python runner.py --sync\n")

    steps = service.uninstall([service.sync_unit()])
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
