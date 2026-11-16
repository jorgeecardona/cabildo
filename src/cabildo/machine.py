"""CPU, thermal and power readings for this machine.

``procs.py`` answers "who holds the RAM". This module answers the same question for
CPU time, and adds what ``powertop`` shows: battery draw, CPU package power, heat.

Linux has many hardware layouts, so every reader here is optional. A reader returns
``None`` when its source is missing or not readable (RAPL energy counters are root-only on
many distributions). It never raises and never invents a number. The consumers show
a panel only when its reading exists.

Sources:

  * CPU stall: ``/proc/pressure/cpu`` (PSI), the share of the last minute in which a task
    waited for a core. The load average is the fallback.
  * Battery: ``/sys/class/power_supply/BAT*``.
  * Package power: Intel RAPL ``energy_uj``, as a rate between two samples.
  * Throttling: ``thermal_throttle/*_throttle_count``, Intel only.

macOS has none of these paths, so only the load average and core count are filled there.
Nothing here kills or changes anything.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import procs

TICKS = procs.TICKS
SYS_CPU = "/sys/devices/system/cpu"
SYS_POWER = "/sys/class/power_supply"
SYS_HWMON = "/sys/class/hwmon"
SYS_RAPL = "/sys/class/powercap"

# Per-process floor for "using CPU": a quarter of one core, averaged since the last sample.
HOG_MIN_PCT = 25.0
# Shortest window that gives a usable rate. Two reads closer than this say nothing.
MIN_WINDOW_S = 1.0


def _read(path: str) -> str:
    """Contents of a file, stripped, or "" when it is missing or not readable."""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _num(path: str) -> float | None:
    """A number from a file, or None."""
    text = _read(path)
    try:
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Cpu:
    """Static-ish CPU state. ``None`` means the source is absent, never "zero"."""

    cores: int
    load1: float
    load5: float
    load15: float
    stall_pct: float | None = None  # PSI cpu "some" avg60
    freq_mhz: float | None = None  # mean over cores
    max_mhz: float | None = None
    governor: str | None = None
    temp_c: float | None = None

    @property
    def load_per_core(self) -> float:
        """One-minute load average divided by the core count."""
        return self.load1 / self.cores if self.cores else 0.0


@dataclass(frozen=True)
class Battery:
    """The first battery in ``/sys/class/power_supply``."""

    pct: float
    status: str  # Charging, Discharging, Full, Not charging, Unknown
    watts: float | None = None  # current draw (discharging) or charge rate
    energy_wh: float | None = None  # energy left

    @property
    def discharging(self) -> bool:
        """True when the machine runs on the battery."""
        return self.status.lower() == "discharging"

    @property
    def hours_left(self) -> float | None:
        """Estimated hours until empty, or None when not discharging or draw is unknown."""
        if not self.discharging or not self.watts or self.energy_wh is None:
            return None
        return self.energy_wh / self.watts


@dataclass(frozen=True)
class Sample:
    """One reading of the machine. Rates (``busy_pct``, ``package_watts``, hogs) need a
    previous sample, so they are ``None`` or empty on the first one.
    """

    at: float
    cpu: Cpu
    busy_pct: float | None = None  # all cores, since the previous sample
    hogs: tuple[procs.Proc, ...] = ()  # processes using >= HOG_MIN_PCT of a core
    battery: Battery | None = None
    package_watts: float | None = None  # RAPL, when readable
    throttle_events: int = 0  # new thermal throttle events since the previous sample


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #
def cpu_stall_pct() -> float | None:
    """PSI cpu ``some avg60``, or None when the kernel has no CPU pressure file.

    "some" is the share of the last minute in which at least one task waited for a core.
    (CPU has no "full" line at system level on older kernels.)
    """
    for line in _read("/proc/pressure/cpu").splitlines():
        parts = line.split()
        if parts and parts[0] == "some":
            for tok in parts[1:]:
                key, _, val = tok.partition("=")
                if key == "avg60":
                    try:
                        return float(val)
                    except ValueError:
                        return None
    return None


def _freqs() -> tuple[float | None, float | None, str | None]:
    """(mean current MHz, max MHz, governor of cpu0)."""
    cur: list[float] = []
    try:
        names = sorted(n for n in os.listdir(SYS_CPU) if n[3:].isdigit() and n.startswith("cpu"))
    except OSError:
        return None, None, None
    for n in names:
        khz = _num(f"{SYS_CPU}/{n}/cpufreq/scaling_cur_freq")
        if khz:
            cur.append(khz / 1000.0)
    max_khz = _num(f"{SYS_CPU}/cpu0/cpufreq/cpuinfo_max_freq")
    gov = _read(f"{SYS_CPU}/cpu0/cpufreq/scaling_governor") or None
    return (
        sum(cur) / len(cur) if cur else None,
        max_khz / 1000.0 if max_khz else None,
        gov,
    )


_CPU_TEMP_DRIVERS = ("coretemp", "k10temp", "zenpower", "cpu_thermal")


def cpu_temp_c() -> float | None:
    """CPU package temperature from the first known hwmon driver, or None."""
    try:
        entries = sorted(os.listdir(SYS_HWMON))
    except OSError:
        return None
    for h in entries:
        if _read(f"{SYS_HWMON}/{h}/name") in _CPU_TEMP_DRIVERS:
            milli = _num(f"{SYS_HWMON}/{h}/temp1_input")
            if milli is not None:
                return milli / 1000.0
    return None


def cpu() -> Cpu:
    """Current CPU state. Always returns; absent sources stay None."""
    l1, l5, l15 = os.getloadavg()
    freq, max_mhz, gov = _freqs() if sys.platform == "linux" else (None, None, None)
    return Cpu(
        cores=os.cpu_count() or 1,
        load1=l1,
        load5=l5,
        load15=l15,
        stall_pct=cpu_stall_pct() if sys.platform == "linux" else None,
        freq_mhz=freq,
        max_mhz=max_mhz,
        governor=gov,
        temp_c=cpu_temp_c() if sys.platform == "linux" else None,
    )


def battery() -> Battery | None:
    """The first battery, or None on a desktop, a VM, or macOS."""
    try:
        names = sorted(os.listdir(SYS_POWER))
    except OSError:
        return None
    for n in names:
        base = f"{SYS_POWER}/{n}"
        if _read(f"{base}/type") != "Battery":
            continue
        status = _read(f"{base}/status") or "Unknown"
        pct = _num(f"{base}/capacity")
        if pct is None:
            now, full = _num(f"{base}/energy_now"), _num(f"{base}/energy_full")
            if not now or not full:
                continue
            pct = 100.0 * now / full
        uw = _num(f"{base}/power_now")
        if uw is None:
            ua, uv = _num(f"{base}/current_now"), _num(f"{base}/voltage_now")
            uw = ua * uv / 1e6 if ua is not None and uv is not None else None
        energy = _num(f"{base}/energy_now")
        return Battery(
            pct=pct,
            status=status,
            watts=uw / 1e6 if uw is not None else None,
            energy_wh=energy / 1e6 if energy is not None else None,
        )
    return None


def rapl_energy_uj() -> int | None:
    """Sum of the package-level RAPL energy counters in microjoules, or None.

    None when the machine has no RAPL, or the file is not readable (root-only on
    kernels since 5.10 as a fix for the PLATYPUS side channel).
    """
    try:
        names = sorted(os.listdir(SYS_RAPL))
    except OSError:
        return None
    total, seen = 0, False
    for n in names:
        # Top-level package domains only: "intel-rapl:0", not "intel-rapl:0:0" (sub-domain).
        if not n.startswith("intel-rapl:") or n.count(":") != 1:
            continue
        text = _read(f"{SYS_RAPL}/{n}/energy_uj")
        if text.isdigit():
            total += int(text)
            seen = True
    return total if seen else None


def _rapl_range_uj() -> int:
    """Wrap-around point of the counter (``max_energy_range_uj``), 0 when unknown."""
    total = 0
    try:
        names = sorted(os.listdir(SYS_RAPL))
    except OSError:
        return 0
    for n in names:
        if n.startswith("intel-rapl:") and n.count(":") == 1:
            text = _read(f"{SYS_RAPL}/{n}/max_energy_range_uj")
            total += int(text) if text.isdigit() else 0
    return total


def throttle_count() -> int | None:
    """Total thermal throttle events since boot over all cores and packages, or None."""
    try:
        names = [n for n in os.listdir(SYS_CPU) if n.startswith("cpu") and n[3:].isdigit()]
    except OSError:
        return None
    total, seen = 0, False
    for n in names:
        for kind in ("core", "package"):
            v = _num(f"{SYS_CPU}/{n}/thermal_throttle/{kind}_throttle_count")
            if v is not None:
                total += int(v)
                seen = True
    return total if seen else None


def cpu_ticks() -> tuple[int, int] | None:
    """(busy, total) jiffies over all cores from ``/proc/stat``, or None."""
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            try:
                f = [int(x) for x in line.split()[1:]]
            except ValueError:
                return None
            idle = f[3] + (f[4] if len(f) > 4 else 0)  # idle + iowait
            return sum(f[:8]) - idle, sum(f[:8])
    return None


def _proc_ticks(pid: int) -> tuple[str, int, int] | None:
    """(comm, ppid, utime+stime in ticks) for one pid, or None when it is gone."""
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    rparen = data.rfind(b")")
    if rparen == -1:
        return None
    comm = data[data.find(b"(") + 1 : rparen].decode("utf-8", "replace")
    f = data[rparen + 2 :].split()
    try:
        # After comm: state is f[0], ppid f[1], utime f[11], stime f[12].
        return comm, int(f[1]), int(f[11]) + int(f[12])
    except (IndexError, ValueError):
        return None


def _all_ticks() -> dict[int, tuple[str, int, int]]:
    out: dict[int, tuple[str, int, int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for e in entries:
        if e.isdigit():
            t = _proc_ticks(int(e))
            if t is not None:
                out[int(e)] = t
    return out


# --------------------------------------------------------------------------- #
# Sampler
# --------------------------------------------------------------------------- #
class Sampler:
    """Takes ``Sample``s and keeps the previous raw counters, so rates can be computed.

    One instance per process. The daemon keeps one for its sweeps; ``cabildo load`` takes
    two readings a moment apart.
    """

    def __init__(self) -> None:
        self._t: float | None = None
        self._ticks: tuple[int, int] | None = None
        self._procs: dict[int, tuple[str, int, int]] = {}
        self._rapl: int | None = None
        self._throttle: int | None = None

    def sample(self) -> Sample:
        """Read everything now. Rates are relative to the previous call."""
        now = time.monotonic()
        c = cpu()
        window = now - self._t if self._t is not None else 0.0
        have_window = window >= MIN_WINDOW_S

        busy_pct: float | None = None
        ticks = cpu_ticks() if sys.platform == "linux" else None
        if have_window and ticks and self._ticks and ticks[1] > self._ticks[1]:
            busy_pct = 100.0 * (ticks[0] - self._ticks[0]) / (ticks[1] - self._ticks[1])

        cur_procs = _all_ticks() if sys.platform == "linux" else {}
        hogs: list[procs.Proc] = []
        if have_window:
            for pid, (comm, ppid, t) in cur_procs.items():
                prev = self._procs.get(pid)
                if prev is None:
                    continue
                pct = 100.0 * (t - prev[2]) / TICKS / window
                if pct >= HOG_MIN_PCT:
                    hogs.append(
                        procs.Proc(
                            pid=pid,
                            ppid=ppid,
                            comm=comm,
                            rss_kb=procs._rss_kb(pid),
                            started_at=0.0,
                            cmdline=procs._cmdline(pid),
                            cpu_pct=pct,
                        )
                    )
            hogs.sort(key=lambda p: p.cpu_pct, reverse=True)

        rapl = rapl_energy_uj() if sys.platform == "linux" else None
        watts: float | None = None
        if have_window and rapl is not None and self._rapl is not None:
            delta = rapl - self._rapl
            if delta < 0:  # the counter wrapped around
                delta += _rapl_range_uj()
            if delta >= 0:
                watts = delta / 1e6 / window

        thr = throttle_count() if sys.platform == "linux" else None
        events = (
            max(0, thr - self._throttle) if thr is not None and self._throttle is not None else 0
        )

        self._t, self._ticks, self._procs, self._rapl, self._throttle = (
            now,
            ticks,
            cur_procs,
            rapl,
            thr,
        )
        return Sample(
            at=time.time(),
            cpu=c,
            busy_pct=busy_pct,
            hogs=tuple(hogs),
            battery=battery() if sys.platform == "linux" else None,
            package_watts=watts,
            throttle_events=events,
        )
