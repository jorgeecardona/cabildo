"""What this machine is doing with its RAM — and *which agent* is doing it.

A plain "memory is at 94%" is anxiety, not information: every agent on the bus
reads it and none of them learns whether to act. cabildo can do better, because
it is the only thing on the machine that knows the map from processes to agents:

  * **ancestry** — every tool call an agent makes is a descendant of its own
    `claude`, and `sessions.claude_pid` binds that pid to a session. So an 8 GB
    pytest resolves to "neon-claptrap is holding 8 GB", and the nudge that lands
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

Linux /proc only, stdlib only (the hooks import this transitively). Every reader
degrades to empty rather than raising: a memory monitor that can crash a session
is worse than no memory monitor.
"""

from __future__ import annotations

import os
import re
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
        alerting always, which means alerting never."""
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


def pressure() -> dict[str, float]:
    """Kernel memory PSI, or {} where it isn't compiled in.

    Percentages lie about *load*: a box can sit at 5% available and be perfectly
    happy, or at 25% and be thrashing itself to death. PSI measures the thing we
    actually care about — wall time lost waiting on memory. `full avg60` is time
    when every runnable task was stalled, which is the machine being unusable,
    and being a 60-second average it does not flap."""
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


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #
@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    rss_kb: int
    started_at: float                    # epoch seconds
    cmdline: str = ""
    session_id: str | None = None        # owning agent, once attributed
    claude_pid: int = 0
    via: str = ""                        # 'path' | 'ancestry' — how we attributed it

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
    line. This runs for every pid on the box, so it is the hot path."""
    try:
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * PAGE_KB
    except (OSError, IndexError, ValueError):
        return 0


def _stat(pid: int) -> tuple[str, int, float] | None:
    """(comm, ppid, starttime_ticks). comm is parenthesized and may contain
    spaces and parens itself, so index off the final ')'."""
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
    """Every process at or above `min_rss_kb`, biggest first.

    Filtered on RSS *before* reading cmdline: the RSS read is one line per pid
    and cheap enough for all ~500 of them, the cmdline read is not worth doing
    for the 480 that hold a few megabytes."""
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
            continue          # rss == 0: kernel thread, or it exited under us
        st = _stat(pid)
        if st is None:
            continue
        comm, ppid, start_ticks = st
        out.append(Proc(
            pid=pid, ppid=ppid, comm=comm, rss_kb=rss,
            started_at=boot + start_ticks / TICKS if boot else 0.0,
            cmdline=_cmdline(pid),
        ))
    out.sort(key=lambda p: p.rss_kb, reverse=True)
    return out


def attribute(procs: list[Proc], *, pid_to_session: dict[int, str] | None = None) -> None:
    """Fill in `session_id` on each proc, in place.

    Path first, ancestry second — and the order is the whole point. Ancestry can
    only ever find a *live* claude, so on its own it reports the fleet's current
    load and is structurally blind to what dead agents left running. The path
    route sees exactly that, which is the case worth alerting on.

    `pid_to_session` maps claude pids to session ids (from the bus). Without it,
    ancestry still resolves a pid, and the caller can map it later."""
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
        signal must read as "no evidence", never as "it is bad"."""
        return self.pressure.get("full60", 0.0)

    def by_session(self) -> dict[str, list[Proc]]:
        out: dict[str, list[Proc]] = {}
        for p in self.procs:
            if p.session_id:
                out.setdefault(p.session_id, []).append(p)
        return out

    def rss_kb_of(self, session_id: str) -> int:
        return sum(p.rss_kb for p in self.procs if p.session_id == session_id)


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
    earn. Attributed-and-alive is also left out: that is the fleet working."""
    is_alive = alive or _alive
    out = []
    for p in snap.procs:
        if not p.session_id and not p.claude_pid:
            continue                                  # unattributable: not ours to judge
        if p.session_id and p.session_id in live_sessions:
            continue
        if p.via == "ancestry" and is_alive(p.claude_pid):
            continue                                  # its agent is right there, working
        out.append(p)
    return out


_SELF_RE = re.compile(r"\b(cabildo|systemd)\b")


def is_self(p: Proc) -> bool:
    """cabildo's own daemon must never appear in its own list of things to kill."""
    return bool(_SELF_RE.search(p.comm)) or "cabildo" in p.cmdline.split(" ")[0]
