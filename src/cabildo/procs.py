"""What this machine is doing with its RAM — and *which agent* is doing it.

A plain "memory is at 94%" is anxiety, not information: every agent on the bus
reads it and none of them learns whether to act. cabildo can do better, because
it is the only thing on the machine that knows the map from processes to agents:

  * **ancestry** — every tool call an agent makes is a descendant of its own
    `claude`, and `sessions.claude_pid` binds that pid to a session. So an 8 GB
    pytest resolves to "neon-claptrap is holding 8 GB", and the nudge that pushes
    in neon-claptrap's context can say *you*, while everyone else is told to
    carry on.

  * **the scratchpad path** — `/tmp/claude-<uid>/<cwd>/<session-id>/…` names the
    session that owns it, and unlike ancestry it survives the agent. A venv or
    worktree built under a scratchpad keeps that id in its children's argv after
    the `claude` that made it is gone, which is exactly when you want to know.

The second route is what makes "loose process" provable rather than a guess.
A big process whose owning session has ended is not a heuristic about orphans:
it is a process whose requester is definitively not coming back for the result.
That is worth telling the fleet about. A big process we cannot attribute is
somebody's browser, and we say nothing about it — a nudge that cries wolf over
Chrome gets the whole mechanism turned off.

Nothing here kills anything, ever. It reports; humans and agents decide.

Linux reads /proc directly -- fast, stdlib only (the hooks import this
transitively). Darwin has no /proc, so its backends shell out to `ps`,
`vm_stat`, and `sysctl` instead; same contract, one process spawn per snapshot
instead of zero. Every reader still degrades to empty rather than raising: a
memory monitor that can crash a session is worse than no memory monitor.

PSI (kernel memory-pressure stall time) has no darwin equivalent -- macOS only
exposes a discrete, instantaneous level (`kern.memorystatus_vm_pressure_level`:
NORMAL/WARN/CRITICAL), not a rolling stall-time average. `pressure()` reports
{} there rather than fake one; see `_pressure_darwin` for why that is the
honest choice, not a stub waiting to be filled in.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .context import resolve_claude_pid, scratchpad_session

PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
TICKS = os.sysconf("SC_CLK_TCK") or 100


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _run(*args: str) -> str:
    """Stdout of a best-effort subprocess call, or "" on any failure.

    The darwin backends have no /proc to read, so this is their `_read()`:
    a missing binary, a non-zero exit, or a hang past the timeout all degrade
    to empty rather than raising or blocking a snapshot forever.
    """
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout if cp.returncode == 0 else ""


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Memory:
    total_kb: int = 0
    available_kb: int = 0
    swap_total_kb: int = 0
    swap_free_kb: int = 0

    @property
    def ok(self) -> bool:
        return self.total_kb > 0

    @property
    def avail_pct(self) -> float:
        """Percent of RAM the kernel thinks is still obtainable without swapping.

        MemAvailable, not MemFree: page cache is reclaimable, so a healthy Linux
        box reports MemFree near zero at all times. Alerting on MemFree means
        alerting always, which means alerting never.
        """
        return 100.0 * self.available_kb / self.total_kb if self.total_kb else 100.0

    @property
    def swap_used_pct(self) -> float:
        if not self.swap_total_kb:
            return 0.0
        return 100.0 * (self.swap_total_kb - self.swap_free_kb) / self.swap_total_kb


_MEM_FIELDS = {
    "MemTotal": "total_kb",
    "MemAvailable": "available_kb",
    "SwapTotal": "swap_total_kb",
    "SwapFree": "swap_free_kb",
}


def memory() -> Memory:
    return _memory_darwin() if sys.platform == "darwin" else _memory_linux()


def _memory_linux() -> Memory:
    vals: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        name, _, rest = line.partition(":")
        attr = _MEM_FIELDS.get(name.strip())
        if attr:
            try:
                vals[attr] = int(rest.split()[0])
            except (IndexError, ValueError):
                pass
    return Memory(**vals)


# vm_stat prints "<label padded to 30>  <value padded to 16>.\n" -- trailing
# period included -- after a header line naming the page size (16 KiB on
# Apple Silicon, 4 KiB on Intel; never hardcode it).
_VM_STAT_FIELDS = {"Pages free": "free", "Pages inactive": "inactive"}
_VM_STAT_PAGESIZE_RE = re.compile(r"page size of (\d+) bytes")
_SWAPUSAGE_RE = re.compile(r"total\s*=\s*([\d.]+)M\s+used\s*=\s*([\d.]+)M\s+free\s*=\s*([\d.]+)M")


def _memory_darwin() -> Memory:
    """`sysctl` for the totals /proc/meminfo would give us, `vm_stat` for the
    breakdown MemAvailable is built from.

    "Available" has no darwin counterpart either, so rather than invent one
    this mirrors psutil's cross-platform formula: `inactive + free` pages.
    Inactive is darwin's nearest analogue of Linux's reclaimable page cache --
    not yet needed, but not pinned down as "in use" either -- and free is
    already free. `purgeable` and `speculative` are deliberately left out, same
    as psutil (github.com/giampaolo/psutil, psutil/arch/osx/mem.c; see also
    psutil issue #1277 for why speculative doesn't count).
    """
    total_kb = 0
    raw_total = _run("sysctl", "-n", "hw.memsize").strip()
    if raw_total.isdigit():
        total_kb = int(raw_total) // 1024

    vm_text = _run("vm_stat")
    size_match = _VM_STAT_PAGESIZE_RE.search(vm_text)
    page_kb = int(size_match.group(1)) / 1024 if size_match else 4.0
    pages: dict[str, int] = {}
    for line in vm_text.splitlines():
        name, sep, rest = line.partition(":")
        key = _VM_STAT_FIELDS.get(name.strip()) if sep else None
        if key:
            try:
                pages[key] = int(rest.strip().rstrip("."))
            except ValueError:
                pass
    available_kb = round((pages.get("free", 0) + pages.get("inactive", 0)) * page_kb)

    swap_total_kb = swap_free_kb = 0
    swap_match = _SWAPUSAGE_RE.search(_run("sysctl", "-n", "vm.swapusage"))
    if swap_match:
        swap_total_kb = round(float(swap_match.group(1)) * 1024)
        swap_free_kb = round(float(swap_match.group(3)) * 1024)

    return Memory(
        total_kb=total_kb,
        available_kb=available_kb,
        swap_total_kb=swap_total_kb,
        swap_free_kb=swap_free_kb,
    )


def pressure() -> dict[str, float]:
    return _pressure_darwin() if sys.platform == "darwin" else _pressure_linux()


def _pressure_linux() -> dict[str, float]:
    """Kernel memory PSI, or {} where it isn't compiled in.

    Percentages lie about *load*: a box can sit at 5% available and be perfectly
    happy, or at 25% and be thrashing itself to death. PSI measures the thing we
    actually care about — wall time lost waiting on memory. `full avg60` is time
    when every runnable task was stalled, which is the machine being unusable,
    and being a 60-second average it does not flap.
    """
    out: dict[str, float] = {}
    for line in _read("/proc/pressure/memory").splitlines():
        kind = line.split(None, 1)[0] if line.split() else ""
        for tok in line.split()[1:]:
            k, _, v = tok.partition("=")
            if k.startswith("avg"):
                try:
                    out[f"{kind}{k[3:]}"] = float(v)
                except ValueError:
                    pass
    return out


def _pressure_darwin() -> dict[str, float]:
    """Always {} -- deliberately, not "not implemented yet".

    The nearest darwin has to PSI is `kern.memorystatus_vm_pressure_level` (the
    same signal the `memory_pressure` CLI and dispatch_source_memorypressure
    read): a discrete, instantaneous NORMAL/WARN/CRITICAL level, not a rolling
    stall-time average. Synthesizing an `avgN`-shaped float from a 3-value
    snapshot would look like PSI without meaning what PSI means, and a monitor
    that lies about load is worse than one that says nothing (see the module
    docstring). `Snapshot.stall_pct` already treats {} as "no evidence", so
    `monitor.level_for` falls back to `avail_pct` alone on darwin — honest, if
    less sensitive than Linux.
    """
    return {}


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #
@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    rss_kb: int
    started_at: float  # epoch seconds
    cmdline: str = ""
    session_id: str | None = None  # owning agent, once attributed
    claude_pid: int = 0
    via: str = ""  # 'path' | 'ancestry' — how we attributed it
    cpu_pct: float = 0.0  # share of one core since the last sample; 0 when not measured

    @property
    def rss_mb(self) -> int:
        return self.rss_kb // 1024

    def label(self, width: int = 60) -> str:
        """A line a human can act on: what it is, not just its comm."""
        text = " ".join((self.cmdline or self.comm).split())
        # Strip the interpreter/venv prefix — `…/.venv/bin/python3 …/bin/pytest -q
        # tests/x.py` says pytest tests/x.py, and the 90 characters in front of
        # that are the part nobody reads.
        parts = [os.path.basename(p) if p.startswith("/") else p for p in text.split(" ")]
        text = " ".join(parts) or self.comm
        return text if len(text) <= width else text[: width - 1] + "…"


def _boot_time() -> float:
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def _rss_kb(pid: int) -> int:
    """Resident pages from statm — one small read, no parsing of the big stat
    line. This runs for every pid on the box, so it is the hot path.
    """
    try:
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * PAGE_KB
    except (OSError, IndexError, ValueError):
        return 0


def _stat(pid: int) -> tuple[str, int, float] | None:
    """(comm, ppid, starttime_ticks). comm is parenthesized and may contain
    spaces and parens itself, so index off the final ')'.
    """
    data = b""
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
        # fields after comm are stat(5) fields 3..; ppid is 4th, starttime 22nd.
        return comm, int(f[1]), float(f[19])
    except (IndexError, ValueError):
        return None


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a)


def _cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def processes(min_rss_kb: int = 0) -> list[Proc]:
    return (
        _processes_darwin(min_rss_kb) if sys.platform == "darwin" else _processes_linux(min_rss_kb)
    )


def _processes_linux(min_rss_kb: int = 0) -> list[Proc]:
    """Every process at or above `min_rss_kb`, biggest first.

    Filtered on RSS *before* reading cmdline: the RSS read is one line per pid
    and cheap enough for all ~500 of them, the cmdline read is not worth doing
    for the 480 that hold a few megabytes.
    """
    boot = _boot_time()
    out: list[Proc] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        rss = _rss_kb(pid)
        if rss < min_rss_kb or rss == 0:
            continue  # rss == 0: kernel thread, or it exited under us
        st = _stat(pid)
        if st is None:
            continue
        comm, ppid, start_ticks = st
        out.append(
            Proc(
                pid=pid,
                ppid=ppid,
                comm=comm,
                rss_kb=rss,
                started_at=boot + start_ticks / TICKS if boot else 0.0,
                cmdline=_cmdline(pid),
            )
        )
    out.sort(key=lambda p: p.rss_kb, reverse=True)
    return out


# ps's ELAPSED column: "[[dd-]hh:]mm:ss" -- there is no darwin `etimes` (that's
# a Linux/procps-only extension), so we parse the colon-separated form and
# turn it into an absolute started_at ourselves, the same shape /proc gives us.
_ETIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


def _parse_etime(text: str) -> float:
    """`ps -o etime=` as seconds elapsed. Anything unparseable -> 0.0 (treated
    the same as "unknown start time" already is on Linux: age reads as 0).
    """
    m = _ETIME_RE.match(text.strip())
    if not m:
        return 0.0
    days, hours, mins, secs = m.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(mins) * 60 + float(secs)


def _processes_darwin(min_rss_kb: int = 0) -> list[Proc]:
    """One `ps` call for every process: pid, ppid, rss, elapsed time, and the
    full command line, in that column order so a `split(None, 4)` cleanly
    separates the four fixed fields from the command (which may itself
    contain spaces).

    No per-pid syscalls the way the /proc walk needs -- `ps` already did that
    walk -- so the tradeoff is one process spawn per snapshot instead of zero.
    `rss` is already in 1024-byte units, same convention as /proc/<pid>/statm.
    """
    out: list[Proc] = []
    now = time.time()
    for line in _run("ps", "-axo", "pid=,ppid=,rss=,etime=,args=").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if rss < min_rss_kb or rss == 0:
            continue
        args = parts[4] if len(parts) > 4 else ""
        comm = os.path.basename(args.split(" ", 1)[0]) if args else ""
        out.append(
            Proc(
                pid=pid,
                ppid=ppid,
                comm=comm,
                rss_kb=rss,
                started_at=now - _parse_etime(parts[3]),
                cmdline=args,
            )
        )
    out.sort(key=lambda p: p.rss_kb, reverse=True)
    return out


def attribute(procs: list[Proc], *, pid_to_session: dict[int, str] | None = None) -> None:
    """Fill in `session_id` on each proc, in place.

    Path first, ancestry second — and the order is the whole point. Ancestry can
    only ever find a *live* claude, so on its own it reports the fleet's current
    load and is structurally blind to what dead agents left running. The path
    route sees exactly that, which is the case worth alerting on.

    `pid_to_session` maps claude pids to session ids (from the bus). Without it,
    ancestry still resolves a pid, and the caller can map it later.

    On darwin, `_cwd` and `resolve_claude_pid` still read /proc directly (that
    ancestry walk is `context.py`'s, out of this module's scope) and simply
    find nothing there — same "degrade to empty" contract, no crash. So on
    darwin only the path route (cmdline against the scratchpad pattern) can
    attribute a process; ancestry attribution is a follow-up, not something
    this fix claims to cover.
    """
    lookup = pid_to_session or {}
    for p in procs:
        sid = scratchpad_session(p.cmdline) or scratchpad_session(_cwd(p.pid))
        if sid:
            p.session_id, p.via = sid, "path"
            continue
        cp = resolve_claude_pid(p.pid, include_self=True)
        if cp:
            p.claude_pid, p.via = cp, "ancestry"
            p.session_id = lookup.get(cp)


@dataclass
class Snapshot:
    memory: Memory
    pressure: dict[str, float] = field(default_factory=dict)
    procs: list[Proc] = field(default_factory=list)

    @property
    def stall_pct(self) -> float:
        """PSI `full avg60`: percent of the last minute in which *everything*
        was stalled on memory. 0.0 when PSI is unavailable — absence of the
        signal must read as "no evidence", never as "it is bad".
        """
        return self.pressure.get("full60", 0.0)

    def by_session(self) -> dict[str, list[Proc]]:
        out: dict[str, list[Proc]] = {}
        for p in self.procs:
            if p.session_id:
                out.setdefault(p.session_id, []).append(p)
        return out


def snapshot(min_rss_mb: int = 100, *, pid_to_session: dict[int, str] | None = None) -> Snapshot:
    procs = processes(min_rss_kb=min_rss_mb * 1024)
    attribute(procs, pid_to_session=pid_to_session)
    return Snapshot(memory=memory(), pressure=pressure(), procs=procs)


# --------------------------------------------------------------------------- #
# Loose processes
# --------------------------------------------------------------------------- #
def _alive(pid: int) -> bool:
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def loose(snap: Snapshot, live_sessions: set[str], *, alive=None) -> list[Proc]:
    """Processes attributable to an agent that is gone.

    Two ways to be gone: the session row is no longer active on the bus, or (for
    a session we have no row for at all) the `claude` that owned it is dead.
    Either way nobody is waiting on the result, so the memory is pure waste.

    Deliberately narrow. Anything we could not attribute is left out entirely —
    it is far likelier to be the human's editor than an agent's leftovers, and
    one nudge about somebody's browser costs more trust than ten correct ones
    earn. Attributed-and-alive is also left out: that is the fleet working.
    """
    is_alive = alive or _alive
    out = []
    for p in snap.procs:
        if not p.session_id and not p.claude_pid:
            continue  # unattributable: not ours to judge
        if p.session_id and p.session_id in live_sessions:
            continue
        if p.via == "ancestry" and is_alive(p.claude_pid):
            continue  # its agent is right there, working
        out.append(p)
    return out


_SELF_RE = re.compile(r"\b(cabildo|systemd)\b")


def is_self(p: Proc) -> bool:
    """Cabildo's own daemon must never appear in its own list of things to kill."""
    return bool(_SELF_RE.search(p.comm)) or "cabildo" in p.cmdline.split(" ")[0]
