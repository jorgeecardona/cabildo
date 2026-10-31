"""usage.py: CPU, memory and wakeups of process trees (/proc)."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from cabildo.workspace import usage
from cabildo.workspace.usage import Proc, Sample

pytestmark = pytest.mark.skipif(not usage.available(), reason="needs Linux /proc")
T = usage.TICKS


def proc(
    pid: int,
    ticks: int = 0,
    slices: int | None = 0,
    child_ticks: int = 0,
    start: int = 1,
    ppid: int = 1,
    anon_kb: int = 0,
    comm: str = "p",
) -> Proc:
    return Proc(
        pid=pid,
        ppid=ppid,
        comm=comm,
        start=start,
        ticks=ticks,
        child_ticks=child_ticks,
        anon_kb=anon_kb,
        slices=slices,
    )


def test_cpu_memory_and_wakeups_between_two_samples() -> None:
    prev = Sample(root=10, at=100.0, procs=(proc(10), proc(11, ppid=10)))
    now = Sample(
        root=10,
        at=102.0,
        procs=(
            proc(10, ticks=T, slices=400, anon_kb=2048),
            proc(11, ticks=T // 2, slices=100, ppid=10, anon_kb=1024),
        ),
    )
    u = usage.use(prev, now)
    assert u.cpu == pytest.approx(75.0)  # 1.5 s of CPU in 2 s
    assert u.wakeups == pytest.approx(250.0)
    assert u.mem_mb == pytest.approx(3.0)
    assert [p.pid for p in u.procs] == [10, 11]  # busiest first


def test_wakeups_not_read_are_unknown() -> None:
    prev = Sample(root=10, at=0.0, procs=(proc(10, slices=None),))
    now = Sample(root=10, at=1.0, procs=(proc(10, ticks=T, slices=None),))
    u = usage.use(prev, now)
    assert u.wakeups is None and u.procs[0].wakeups is None and u.cpu == pytest.approx(100.0)


def test_a_process_that_ended_in_between_still_counts() -> None:
    # the child had used 1 s at the last sample, then 1 s more and ended; the root
    # waited for it, so its cutime grew by the child's whole life: 2 s
    prev = Sample(root=10, at=0.0, procs=(proc(10), proc(11, ticks=T, ppid=10)))
    now = Sample(root=10, at=4.0, procs=(proc(10, child_ticks=2 * T),))
    u = usage.use(prev, now)
    assert u.cpu == pytest.approx(25.0) and u.ended_cpu == pytest.approx(25.0)  # 1 s in 4 s


def test_a_child_never_seen_still_counts_through_its_parent() -> None:
    prev = Sample(root=10, at=0.0, procs=(proc(10),))
    now = Sample(root=10, at=2.0, procs=(proc(10, child_ticks=T),))  # a 1 s tool run
    assert usage.use(prev, now).cpu == pytest.approx(50.0)


def test_a_new_process_counts_all_its_time() -> None:
    prev = Sample(root=10, at=0.0, procs=(proc(10),))
    now = Sample(root=10, at=1.0, procs=(proc(10), proc(12, ticks=T // 2, ppid=10)))
    assert usage.use(prev, now).cpu == pytest.approx(50.0)


def test_a_reused_pid_is_a_new_process() -> None:
    prev = Sample(root=10, at=0.0, procs=(proc(10), proc(12, ticks=10 * T, start=5)))
    now = Sample(root=10, at=1.0, procs=(proc(10), proc(12, ticks=T // 4, start=900)))
    assert usage.use(prev, now).procs[0].cpu == pytest.approx(25.0)  # not 10 s - 0.25 s


def test_formats() -> None:
    assert [usage.fmt_cpu(c) for c in (0, 0.2, 3.4, 150)] == ["0%", "<1%", "3%", "150%"]
    assert [usage.fmt_mb(m) for m in (12.4, 999, 1000, 2560)] == ["12M", "999M", "1.0G", "2.5G"]


# --- reading real processes ---------------------------------------------------------


@pytest.fixture
def tree():
    p = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30 & wait"])
    time.sleep(0.3)
    yield p
    p.kill()
    subprocess.run(["pkill", "-P", str(p.pid)], check=False)
    p.wait()


def test_sample_reads_the_whole_tree(tree) -> None:
    s = usage.sample(tree.pid)
    assert s is not None and s.procs[0].pid == tree.pid
    assert sorted(p.comm for p in s.procs) == ["sh", "sleep", "sleep"]
    assert all(p.anon_kb > 0 for p in s.procs) and all(p.slices is None for p in s.procs)
    alone = usage.sample(tree.pid, with_children=False)
    assert alone is not None and [p.comm for p in alone.procs] == ["sh"]
    detailed = usage.sample(tree.pid, wakeups=True)
    assert detailed is not None and all(p.slices is not None for p in detailed.procs)


def test_a_gone_root_has_no_sample(tree) -> None:
    tree.kill()
    tree.wait()
    assert usage.sample(tree.pid) is None


def test_a_busy_process_is_measured() -> None:
    busy = subprocess.Popen(
        [sys.executable, "-c", "import time\nt = time.time()\nwhile time.time() - t < 3: pass"]
    )
    try:
        time.sleep(0.3)
        a = usage.sample(busy.pid, wakeups=True)
        time.sleep(1.0)
        b = usage.sample(busy.pid, wakeups=True)
        assert a is not None and b is not None
        u = usage.use(a, b)
        assert 60 < u.cpu < 130  # one core, busy
        assert u.wakeups is not None and u.wakeups > 0
        assert u.mem_mb > 1
    finally:
        busy.kill()
        busy.wait()


THREAD_CHILD = """
import subprocess, threading, time
def spawn():
    subprocess.Popen(["sleep", "30"])     # made by a second thread, which stays: when a
    time.sleep(30)                        # thread ends, Linux gives its children to another
threading.Thread(target=spawn).start()
time.sleep(30)
"""


def test_a_child_made_by_another_thread_is_found_by_the_full_walk_and_then_kept() -> None:
    parent = subprocess.Popen([sys.executable, "-c", THREAD_CHILD])
    try:
        time.sleep(0.8)
        cheap = usage.sample(parent.pid, full=False)
        full = usage.sample(parent.pid, full=True)
        assert cheap is not None and full is not None
        assert len(cheap.procs) == 1  # the main thread made no child
        assert [p.comm for p in full.procs][1:] == ["sleep"]
        kept = usage.sample(parent.pid, full=False, known={p.pid: p.start for p in full.procs})
        assert kept is not None and len(kept.procs) == len(full.procs) == 2
    finally:
        subprocess.run(["pkill", "-P", str(parent.pid)], check=False)
        parent.kill()
        parent.wait()


def test_sample_all_walks_fully_every_few_rounds(tree, monkeypatch) -> None:
    walks: list[bool] = []
    real = usage.sample

    def spy(root, with_children=True, *, wakeups=False, full=True, known=None):
        walks.append(full)
        return real(root, with_children, wakeups=wakeups, full=full, known=known)

    monkeypatch.setattr(usage, "sample", spy)
    r = usage.EMPTY
    for _ in range(usage.FULL_WALK_ROUNDS + 1):
        r = usage.sample_all({tree.pid: True}, r)
    assert walks == [True] + [False] * (usage.FULL_WALK_ROUNDS - 1) + [True]
    assert tree.pid in r.uses and r.uses[tree.pid].wakeups is None  # not asked for


def test_sample_all_drops_gone_trees_and_reused_roots(tree) -> None:
    first = usage.sample_all({tree.pid: True}, usage.EMPTY)
    other = usage.Round(
        samples={tree.pid: Sample(root=tree.pid, at=0.0, procs=(proc(tree.pid, start=1),))}, uses={}
    )
    assert usage.sample_all({tree.pid: True}, other).uses == {}  # another process now
    tree.kill()
    tree.wait()
    gone = usage.sample_all({tree.pid: True}, first)
    assert gone.samples == {} and gone.uses == {}
