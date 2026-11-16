"""CPU, thermal and power readers, and the conditions built on them.

The contract: every reader returns None when its source is absent (never a made-up
zero), rates come from two samples, and the conditions use hysteresis.
"""

from __future__ import annotations

import pytest

from cabildo import machine, monitor, notify, procs, store
from tests.conftest import make_session

TH = monitor.Thresholds()


def _cpu(stall: float | None = None, load1: float = 1.0, cores: int = 16) -> machine.Cpu:
    return machine.Cpu(cores=cores, load1=load1, load5=load1, load15=load1, stall_pct=stall)


def _sample(cpu: machine.Cpu, **kw) -> machine.Sample:
    return machine.Sample(at=0.0, cpu=cpu, **kw)


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #
def _fake_fs(monkeypatch, files: dict[str, str]) -> None:
    monkeypatch.setattr(machine, "_read", lambda path: files.get(path, ""))


def test_stall_is_none_without_psi(monkeypatch):
    _fake_fs(monkeypatch, {})
    assert machine.cpu_stall_pct() is None


def test_stall_reads_the_some_avg60(monkeypatch):
    _fake_fs(
        monkeypatch,
        {"/proc/pressure/cpu": "some avg10=1.00 avg60=42.50 avg300=3.00 total=9\n"},
    )
    assert machine.cpu_stall_pct() == 42.5


def test_cpu_ticks_split_busy_from_idle(monkeypatch):
    # user nice system idle iowait irq softirq steal
    _fake_fs(monkeypatch, {"/proc/stat": "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 1 1 1 1\n"})
    assert machine.cpu_ticks() == (150, 1000)


def test_battery_none_when_no_battery(monkeypatch):
    monkeypatch.setattr(machine.os, "listdir", lambda _p: ["AC"])
    _fake_fs(monkeypatch, {f"{machine.SYS_POWER}/AC/type": "Mains"})
    assert machine.battery() is None


def test_battery_derives_watts_from_current_and_voltage(monkeypatch):
    base = f"{machine.SYS_POWER}/BAT0"
    monkeypatch.setattr(machine.os, "listdir", lambda _p: ["BAT0"])
    _fake_fs(
        monkeypatch,
        {
            f"{base}/type": "Battery",
            f"{base}/status": "Discharging",
            f"{base}/capacity": "80",
            f"{base}/current_now": "1000000",  # 1 A, in microamperes
            f"{base}/voltage_now": "12000000",  # 12 V, in microvolts
            f"{base}/energy_now": "36000000",  # 36 Wh, in microwatt-hours
        },
    )
    b = machine.battery()
    assert b is not None
    assert b.watts == pytest.approx(12.0)
    assert b.hours_left == pytest.approx(3.0)


def test_battery_hours_left_needs_discharging():
    assert machine.Battery(pct=90, status="Charging", watts=10, energy_wh=50).hours_left is None
    assert (
        machine.Battery(pct=90, status="Discharging", watts=None, energy_wh=50).hours_left is None
    )


def test_rapl_none_when_unreadable(monkeypatch):
    monkeypatch.setattr(machine.os, "listdir", lambda _p: ["intel-rapl:0", "intel-rapl:0:0"])
    _fake_fs(monkeypatch, {})  # a root-only file reads as ""
    assert machine.rapl_energy_uj() is None


def test_rapl_sums_top_level_domains_only(monkeypatch):
    monkeypatch.setattr(machine.os, "listdir", lambda _p: ["intel-rapl:0", "intel-rapl:0:0"])
    root = machine.SYS_RAPL
    _fake_fs(
        monkeypatch,
        {f"{root}/intel-rapl:0/energy_uj": "1000", f"{root}/intel-rapl:0:0/energy_uj": "400"},
    )
    assert machine.rapl_energy_uj() == 1000


def test_sampler_first_sample_has_no_rates():
    s = machine.Sampler().sample()
    assert s.busy_pct is None
    assert s.hogs == ()
    assert s.package_watts is None
    assert s.throttle_events == 0


# --------------------------------------------------------------------------- #
# Levels
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stall,expected", [(5, None), (35, "warn"), (70, "critical")])
def test_cpu_level_follows_psi(stall, expected):
    assert monitor.cpu_level(_cpu(stall=stall), TH) == expected


def test_cpu_level_falls_back_to_load_per_core():
    assert monitor.cpu_level(_cpu(load1=4.0), TH) is None  # 0.25 per core
    assert monitor.cpu_level(_cpu(load1=32.0), TH) == "warn"  # 2.0 per core
    assert monitor.cpu_level(_cpu(load1=64.0), TH) == "critical"  # 4.0 per core


def test_cpu_recovery_must_clear_the_threshold_by_a_margin():
    just_under = _cpu(stall=TH.cpu_psi_warn - 1)
    assert monitor.cpu_level(just_under, TH) is None
    assert monitor.cpu_level(just_under, TH, sticky="warn") == "warn"
    well_under = _cpu(stall=TH.cpu_psi_warn - TH.cpu_clear_margin - 1)
    assert monitor.cpu_level(well_under, TH, sticky="warn") is None


def test_power_is_silent_on_ac_and_without_a_battery():
    assert monitor.power_level(None, TH) is None
    assert monitor.power_level(machine.Battery(pct=5, status="Charging"), TH) is None


@pytest.mark.parametrize("pct,expected", [(60, None), (25, "warn"), (10, "critical")])
def test_power_level_follows_battery_when_discharging(pct, expected):
    bat = machine.Battery(pct=pct, status="Discharging")
    assert monitor.power_level(bat, TH) == expected


def test_power_recovery_needs_a_margin():
    bat = machine.Battery(pct=TH.battery_warn_pct + 1, status="Discharging")
    assert monitor.power_level(bat, TH) is None
    assert monitor.power_level(bat, TH, sticky="warn") == "warn"


# --------------------------------------------------------------------------- #
# Reconcile and injection
# --------------------------------------------------------------------------- #
def _hog(pid: int, pct: float, sid: str | None) -> procs.Proc:
    return procs.Proc(
        pid=pid, ppid=1, comm="pytest", rss_kb=1, started_at=0.0, cmdline="pytest -n 16",
        session_id=sid, cpu_pct=pct,
    )  # fmt: skip


def test_cpu_condition_tells_the_agent_that_uses_the_cpu(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    make_session(conn, "a" * 8)
    make_session(conn, "b" * 8, claude_pid=2000)
    sample = _sample(_cpu(stall=50), hogs=(_hog(7, 800.0, "a" * 8),))
    log = monitor._reconcile_cpu(conn, sample, TH, "test")
    assert log and "cpu warn" in log[0]

    mine = notify.condition_text(conn, "a" * 8)
    assert mine is not None
    assert "YOU are using 8.0 CPU cores" in mine
    theirs = notify.condition_text(conn, "b" * 8)
    assert theirs is not None
    assert "YOU are" not in theirs


def test_cpu_condition_clears_when_the_load_drops(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    monitor._reconcile_cpu(conn, _sample(_cpu(stall=50)), TH, "test")
    log = monitor._reconcile_cpu(conn, _sample(_cpu(stall=1)), TH, "test")
    assert log == ["✅ cpu cleared"]


def test_power_condition_raises_and_clears(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    low = machine.Battery(pct=20, status="Discharging", watts=10.0, energy_wh=20.0)
    log = monitor._reconcile_power(conn, _sample(_cpu(), battery=low), TH, "test")
    assert log and "power warn" in log[0] and "20%" in log[0]
    plugged = machine.Battery(pct=20, status="Charging")
    log = monitor._reconcile_power(conn, _sample(_cpu(), battery=plugged), TH, "test")
    assert log == ["✅ power cleared"]


def test_thermal_stays_raised_for_the_hold_time(conn, monkeypatch):
    monkeypatch.setattr(store, "hostname", lambda: "box")
    monkeypatch.setattr(monitor, "_last_throttle", 0.0)
    monkeypatch.setattr(monitor.time, "time", lambda: 1000.0)
    log = monitor._reconcile_thermal(conn, _sample(_cpu(), throttle_events=3), TH, "test")
    assert log and "thermal" in log[0]
    # A quiet sample inside the hold time does not clear it.
    monkeypatch.setattr(monitor.time, "time", lambda: 1000.0 + TH.thermal_hold_s - 1)
    assert monitor._reconcile_thermal(conn, _sample(_cpu()), TH, "test") == []
    # After the hold time it clears.
    monkeypatch.setattr(monitor.time, "time", lambda: 1000.0 + TH.thermal_hold_s + 1)
    log = monitor._reconcile_thermal(conn, _sample(_cpu()), TH, "test")
    assert log == ["✅ thermal cleared"]
