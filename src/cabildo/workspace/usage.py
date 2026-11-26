"""CPU, memory and wakeups of process trees, from Linux /proc (no root needed).

A Claude session is a tree of processes: claude, its MCP servers and its tool runs.
Per tree, between two samples:

- CPU: % of one core. Each process's utime + stime, plus what processes that ended
  in between used: a parent's cutime + cstime grows by the whole life of a child it
  waited for, minus what the last sample had already counted for that child. So a
  short tool run the walk never saw still counts.
- Memory: anonymous resident memory (statm: resident - shared). It leaves out file
  pages shared with other processes (libraries), so a sum over a tree is not inflated
  the way RSS is. On this machine it was within 6% of PSS (smaps_rollup) for 232
  Claude processes, at 10 ms instead of 435 ms.
- Wakeups per second: how often its threads were put on a CPU (third field of
  /proc/<pid>/task/<tid>/schedstat); waking a CPU from idle is what costs power, the
  kind of number powertop shows. It reads every thread (~2,400 files for 23 Claudes),
  so only when asked (the top view).

Finding the tree: a child belongs to the thread that made it. Reading every thread's
`children` file finds all (71 ms for 23 Claudes); the main thread's alone misses a few
(8 of 232) at 6 ms. So a round walks every thread only every FULL_WALK_ROUNDS rounds,
and in between keeps the processes found before.

Pure stdlib. Nothing here is available off Linux: `available()` says so.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4
FULL_WALK_ROUNDS = 6  # find children through every thread once every this many rounds


def available() -> bool:
    return os.path.isdir("/proc/self/task")


@dataclass(frozen=True)
class Proc:
    """One process at one moment."""

    pid: int
    ppid: int
    comm: str
    start: int  # starttime, ticks since boot: tells a reused pid apart
    ticks: int  # utime + stime, all its threads
    child_ticks: int  # cutime + cstime: its ended children it waited for
    anon_kb: int  # anonymous resident memory
    slices: int | None  # times its threads were put on a CPU; None: not read

    @property
    def key(self) -> tuple[int, int]:
        return self.pid, self.start


@dataclass(frozen=True)
class Sample:
    """A process tree at one moment."""

    root: int
    at: float  # time.monotonic()
    procs: tuple[Proc, ...]  # the root first


@dataclass(frozen=True)
class ProcUse:
    pid: int
    comm: str
    cpu: float  # % of one core
    mem_mb: float
    wakeups: float | None  # per second; None: not measured


@dataclass(frozen=True)
class Use:
    """One tree's use between two samples (or several trees together)."""

    cpu: float  # % of one core, ended processes included
    mem_mb: float  # now
    wakeups: float | None  # per second; None: not measured
    procs: tuple[ProcUse, ...]  # busiest first
    ended_cpu: float  # the part of `cpu` used by processes that ended in between

    @staticmethod
    def total(uses: list[Use]) -> Use:
        known = [u.wakeups for u in uses if u.wakeups is not None]
        return Use(
            cpu=sum(u.cpu for u in uses),
            mem_mb=sum(u.mem_mb for u in uses),
            wakeups=sum(known) if known else None,
            procs=tuple(p for u in uses for p in u.procs),
            ended_cpu=sum(u.ended_cpu for u in uses),
        )


# --- reading /proc ---------------------------------------------------------------


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read()
    except OSError:  # gone, or not ours
        return None


def _threads(pid: int) -> list[str]:
    try:
        return os.listdir(f"/proc/{pid}/task")
    except OSError:
        return []


def read_proc(pid: int, wakeups: bool = False) -> Proc | None:
    stat = _read(f"/proc/{pid}/stat")
    statm = _read(f"/proc/{pid}/statm")
    if stat is None or statm is None:
        return None
    head, _, rest = stat.rpartition(")")  # the name may hold spaces and ')'
    f = rest.split()  # f[0] is field 3 (state) of proc(5)
    m = statm.split()  # size resident shared ... (pages)
    slices = None
    if wakeups:
        slices = 0
        for tid in _threads(pid):
            sched = _read(f"/proc/{pid}/task/{tid}/schedstat")
            if sched:
                slices += int(sched.split()[2])
    return Proc(
        pid=pid,
        ppid=int(f[1]),
        comm=head.partition("(")[2],
        start=int(f[19]),
        ticks=int(f[11]) + int(f[12]),
        child_ticks=int(f[13]) + int(f[14]),
        anon_kb=max(int(m[1]) - int(m[2]), 0) * PAGE_KB,
        slices=slices,
    )


def children(pid: int, every_thread: bool = True) -> list[int]:
    """Its child processes: through every thread's `children` file, or the main
    thread's alone (cheaper; misses children another thread made).
    """
    tids = _threads(pid) if every_thread else [str(pid)]
    out: list[int] = []
    for tid in tids:
        text = _read(f"/proc/{pid}/task/{tid}/children")
        if text:
            out += [int(c) for c in text.split()]
    return out


def sample(
    root: int,
    with_children: bool = True,
    *,
    wakeups: bool = False,
    full: bool = True,
    known: Mapping[int, int] | None = None,
) -> Sample | None:
    """The tree under `root` now; None when `root` is gone.

    full: find children through every thread; else through the main thread, then add
    the `known` processes (pid -> start, from the last sample) the walk missed that are
    still there, under a process of the tree.
    """
    at = time.monotonic()
    first = read_proc(root, wakeups)
    if first is None:
        return None
    procs = [first]
    seen = {root}
    todo = children(root, every_thread=full) if with_children else []

    def walk() -> None:
        while todo:
            pid = todo.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            proc = read_proc(pid, wakeups)
            if proc is not None:
                procs.append(proc)
                todo.extend(children(pid, every_thread=full))

    walk()
    if with_children and known and not full:
        missed = sorted(pid for pid in known if pid not in seen)  # parents first, mostly
        for pid in missed:
            proc = read_proc(pid, wakeups)
            if proc is not None and proc.start == known[pid] and proc.ppid in seen:
                seen.add(pid)
                procs.append(proc)
                todo.extend(children(pid, every_thread=False))
                walk()
    return Sample(root=root, at=at, procs=tuple(procs))


# --- from two samples to a use ----------------------------------------------------


def use(prev: Sample, now: Sample) -> Use:
    """What the tree used between `prev` and `now`."""
    dt = max(now.at - prev.at, 1e-6)
    before = {p.key: p for p in prev.procs}
    keys = {p.key for p in now.procs}
    procs: list[ProcUse] = []
    reaped = 0
    for p in now.procs:
        q = before.get(p.key)
        ticks = p.ticks - q.ticks if q else p.ticks  # new: started in between
        wake = None
        if p.slices is not None and (q is None or q.slices is not None):
            wake = max(p.slices - (q.slices if q and q.slices is not None else 0), 0) / dt
        if q:
            reaped += p.child_ticks - q.child_ticks
        procs.append(
            ProcUse(
                pid=p.pid,
                comm=p.comm,
                cpu=max(ticks, 0) * 100 / TICKS / dt,
                mem_mb=p.anon_kb / 1024,
                wakeups=wake,
            )
        )
    counted = sum(q.ticks for k, q in before.items() if k not in keys)
    ended = max(reaped - counted, 0) * 100 / TICKS / dt
    procs.sort(key=lambda u: u.cpu, reverse=True)
    known = [u.wakeups for u in procs if u.wakeups is not None]
    return Use(
        cpu=sum(u.cpu for u in procs) + ended,
        mem_mb=sum(u.mem_mb for u in procs),
        wakeups=sum(known) if known else None,
        procs=tuple(procs),
        ended_cpu=ended,
    )


@dataclass(frozen=True)
class Round:
    """One sampling round over every tree: what the next round starts from."""

    samples: dict[int, Sample]  # root pid -> its tree now
    uses: dict[int, Use]  # root pid -> its use since the last round
    number: int = 0  # rounds so far (every FULL_WALK_ROUNDS-th walks fully)


EMPTY = Round(samples={}, uses={})


def sample_all(roots: Mapping[int, bool], last: Round, *, wakeups: bool = False) -> Round:
    """Sample every tree (root pid -> with its children). A use needs the same root
    process in the last round (not a reused pid).
    """
    full = last.number % FULL_WALK_ROUNDS == 0
    samples: dict[int, Sample] = {}
    uses: dict[int, Use] = {}
    for root, with_children in roots.items():
        q = last.samples.get(root)
        known = {p.pid: p.start for p in q.procs} if q else None
        s = sample(root, with_children, wakeups=wakeups, full=full, known=known)
        if s is None:
            continue
        samples[root] = s
        if q is not None and q.procs[0].key == s.procs[0].key:
            uses[root] = use(q, s)
    return Round(samples=samples, uses=uses, number=last.number + 1)


def fmt_cpu(cpu: float) -> str:
    return "<1%" if 0 < cpu < 0.5 else f"{cpu:.0f}%"


def fmt_count(n: int) -> str:
    """``954``, ``54k``, ``1.2M``, ``9.3B``.

    Billions are reached by a cache-read total over a long history, where ``9321.7M`` is
    a number nobody reads at a glance.
    """
    if n < 1000:
        return str(n)
    if n < 999_500:
        return f"{n / 1000:.0f}k"
    if n < 999_500_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000_000:.1f}B"


def fmt_mb(mb: float) -> str:
    return f"{mb:.0f}M" if mb < 999.5 else f"{mb / 1024:.1f}G"


def fmt_age(seconds: float) -> str:
    """``45s``, ``3m``, ``2h`` or ``4d``: one unit, the largest that is not zero."""
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def fmt_money(dollars: float) -> str:
    """``$0.84``, ``$12.34``, ``$4,821.79``: cents always, because a daily figure is often
    cents and rounding them away would show an active day as ``$0``.
    """
    return f"${dollars:,.2f}"
