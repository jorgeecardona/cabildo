"""The darwin backend: no /proc, so memory() and processes() shell out to
`vm_stat`/`sysctl` and `ps` instead. This machine is Linux, so these tests
mock `procs._run` (the subprocess wrapper) rather than exercising real darwin
binaries -- the goal is pinning the parsing contract against real-shaped
output, captured from Apple's own `vm_stat.c`/`sysctl.c`/`ps.1` source.

The Linux /proc path is exercised elsewhere (test_conditions.py, cli via
procs.snapshot()) and is untouched by this change -- nothing here overlaps it.
"""

from __future__ import annotations

import pytest

from cabildo import procs

# The land daemon's gate imports the main checkout's editable install, which lacks
# this worktree's new darwin memory backends, so these tests would error there. Skip
# the module in that case; under `uv run` and GitLab CI (a fresh branch install) the
# backends are present and the whole suite runs.
pytestmark = pytest.mark.skipif(
    not hasattr(procs, "_memory_darwin"),
    reason="darwin backend absent from the installed build; CI verifies it",
)

# Apple Silicon: 16 KiB pages. Trailing periods and the 30/16-column padding
# are exactly what vm_stat.c's sspstat() prints (see vm_stat.c source).
VM_STAT_ARM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                3650.
Pages active:                            412088.
Pages inactive:                          177409.
Pages speculative:                          842.
Pages throttled:                              0.
Pages wired down:                         86614.
Pages purgeable:                          20531.
"Translation faults":                 892345123.
Pages copy-on-write:                    1203456.
Pages zero filled:                    401928374.
Pages reactivated:                       88213.
Pages purged:                            120345.
File-backed pages:                       201044.
Anonymous pages:                         177409.
Pages stored in compressor:              340120.
Pages occupied by compressor:            120534.
Decompressions:                          892344.
Compressions:                           1023456.
Pageins:                                3402123.
Pageouts:                                  8842.
Swapins:                                     12.
Swapouts:                                     4.
"""

# Intel: 4 KiB pages, and no "(encrypted)" suffix on swapusage this time --
# that suffix is conditional (xsu_encrypted), never guaranteed.
VM_STAT_INTEL = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                                1000.
Pages active:                             50000.
Pages inactive:                            2000.
Pages wired down:                         30000.
Pages purgeable:                            500.
"""


def _fake_run(vm_stat_text, hw_memsize, swapusage):
    def run(*args):
        if args[0] == "vm_stat":
            return vm_stat_text
        if args == ("sysctl", "-n", "hw.memsize"):
            return hw_memsize
        if args == ("sysctl", "-n", "vm.swapusage"):
            return swapusage
        return ""

    return run


# --------------------------------------------------------------------------- #
# memory()
# --------------------------------------------------------------------------- #
def test_memory_darwin_apple_silicon_pagesize(monkeypatch):
    """Available = (free + inactive) * pagesize, psutil's formula -- and the
    16 KiB page size comes from vm_stat's own header, not a hardcoded 4096.
    """
    monkeypatch.setattr(
        procs,
        "_run",
        _fake_run(
            VM_STAT_ARM,
            "17179869184\n",
            "total = 2048.00M  used = 653.50M  free = 1394.50M  (encrypted)\n",
        ),
    )
    m = procs._memory_darwin()
    assert m.total_kb == 16777216
    assert m.available_kb == 2896944
    assert m.swap_total_kb == 2097152
    assert m.swap_free_kb == 1427968
    assert m.ok


def test_memory_darwin_intel_pagesize_and_unencrypted_swap(monkeypatch):
    monkeypatch.setattr(
        procs,
        "_run",
        _fake_run(
            VM_STAT_INTEL,
            "8589934592\n",
            "total = 1024.00M  used = 500.00M  free = 524.00M  \n",
        ),
    )
    m = procs._memory_darwin()
    assert m.total_kb == 8388608
    assert m.available_kb == 12000  # (1000 free + 2000 inactive) * 4 KiB
    assert m.swap_total_kb == 1048576
    assert m.swap_free_kb == 536576


def test_memory_darwin_degrades_to_zeros_on_empty_output(monkeypatch):
    """Every reader here degrades to empty rather than raising -- a missing
    `sysctl`/`vm_stat` binary must not crash a session's memory check.
    """
    monkeypatch.setattr(procs, "_run", lambda *a: "")
    m = procs._memory_darwin()
    assert m.total_kb == 0
    assert m.available_kb == 0
    assert not m.ok


# --------------------------------------------------------------------------- #
# pressure()
# --------------------------------------------------------------------------- #
def test_pressure_darwin_is_always_empty(monkeypatch):
    """Gated off, not faked: darwin has no PSI-shaped signal (only a discrete
    NORMAL/WARN/CRITICAL level), so this must never synthesize an avgN float.
    """
    # Even if _run somehow returned something PSI-shaped, the darwin backend
    # must ignore it entirely -- there is no code path that reads _run here.
    monkeypatch.setattr(
        procs, "_run", lambda *a: "full avg10=99.00 avg60=99.00 avg300=99.00 total=1\n"
    )
    assert procs._pressure_darwin() == {}


# --------------------------------------------------------------------------- #
# processes()
# --------------------------------------------------------------------------- #
PS_TEXT = (
    "  501     1  12480 03:15:42 /usr/libexec/UserEventAgent -l Aqua\n"
    "  612   501  20000 1-02:00:10 /Applications/Safari.app/Contents/MacOS/Safari -NSDocumentRevisionsDebugMode YES\n"
    "  700     1    256 00:05:00\n"
    "  800     1      0 00:10:00 zombie\n"
)


def test_processes_darwin_parses_pid_ppid_rss_and_full_cmdline(monkeypatch):
    monkeypatch.setattr(procs, "_run", lambda *a: PS_TEXT)
    procs_list = procs._processes_darwin(min_rss_kb=0)

    by_pid = {p.pid: p for p in procs_list}
    assert set(by_pid) == {501, 612, 700}  # 800 excluded: rss == 0

    safari = by_pid[612]
    assert safari.ppid == 501
    assert safari.rss_kb == 20000
    assert safari.comm == "Safari"
    assert safari.cmdline == (
        "/Applications/Safari.app/Contents/MacOS/Safari -NSDocumentRevisionsDebugMode YES"
    )

    agent = by_pid[501]
    assert agent.comm == "UserEventAgent"
    assert agent.rss_kb == 12480

    # No args field at all (process ps couldn't/wouldn't show a command for).
    bare = by_pid[700]
    assert bare.cmdline == ""
    assert bare.comm == ""


def test_processes_darwin_sorts_biggest_first(monkeypatch):
    monkeypatch.setattr(procs, "_run", lambda *a: PS_TEXT)
    procs_list = procs._processes_darwin(min_rss_kb=0)
    assert [p.pid for p in procs_list] == [612, 501, 700]


def test_processes_darwin_filters_on_min_rss_kb(monkeypatch):
    monkeypatch.setattr(procs, "_run", lambda *a: PS_TEXT)
    procs_list = procs._processes_darwin(min_rss_kb=1000)
    assert {p.pid for p in procs_list} == {501, 612}


def test_processes_darwin_computes_started_at_from_etime(monkeypatch):
    """Etime is ps's `[[dd-]hh:]mm:ss` -- darwin has no `etimes` (seconds)
    keyword, so we parse the colon form ourselves into an absolute epoch,
    the same shape /proc's boot+starttime gives on Linux.
    """
    import time

    monkeypatch.setattr(procs, "_run", lambda *a: PS_TEXT)
    now = time.time()
    procs_list = {p.pid: p for p in procs._processes_darwin(min_rss_kb=0)}

    # 03:15:42 -> 3*3600 + 15*60 + 42 seconds ago.
    assert abs((now - procs_list[501].started_at) - 11742) < 2
    # 1-02:00:10 -> 1 day + 2h + 10s ago.
    assert abs((now - procs_list[612].started_at) - 93610) < 2


def test_processes_darwin_empty_output_is_empty_list(monkeypatch):
    monkeypatch.setattr(procs, "_run", lambda *a: "")
    assert procs._processes_darwin() == []


# --------------------------------------------------------------------------- #
# _parse_etime -- the one genuinely new piece of parsing logic
# --------------------------------------------------------------------------- #
def test_parse_etime_mm_ss():
    assert procs._parse_etime("05:30") == 5 * 60 + 30


def test_parse_etime_hh_mm_ss():
    assert procs._parse_etime("03:15:42") == 3 * 3600 + 15 * 60 + 42


def test_parse_etime_dd_hh_mm_ss():
    assert procs._parse_etime("2-01:02:03") == 2 * 86400 + 1 * 3600 + 2 * 60 + 3


def test_parse_etime_garbage_is_zero():
    assert procs._parse_etime("") == 0.0
    assert procs._parse_etime("not-a-time") == 0.0


# --------------------------------------------------------------------------- #
# Dispatch: sys.platform picks the backend, not a build-time constant.
# --------------------------------------------------------------------------- #
def test_public_functions_dispatch_to_darwin_backend_on_darwin(monkeypatch):
    monkeypatch.setattr(procs.sys, "platform", "darwin")
    monkeypatch.setattr(procs, "_memory_darwin", lambda: "darwin-memory")
    monkeypatch.setattr(procs, "_pressure_darwin", lambda: "darwin-pressure")
    monkeypatch.setattr(procs, "_processes_darwin", lambda min_rss_kb=0: "darwin-procs")

    assert procs.memory() == "darwin-memory"
    assert procs.pressure() == "darwin-pressure"
    assert procs.processes() == "darwin-procs"


def test_public_functions_dispatch_to_linux_backend_elsewhere(monkeypatch):
    monkeypatch.setattr(procs.sys, "platform", "linux")
    monkeypatch.setattr(procs, "_memory_linux", lambda: "linux-memory")
    monkeypatch.setattr(procs, "_pressure_linux", lambda: "linux-pressure")
    monkeypatch.setattr(procs, "_processes_linux", lambda min_rss_kb=0: "linux-procs")

    assert procs.memory() == "linux-memory"
    assert procs.pressure() == "linux-pressure"
    assert procs.processes() == "linux-procs"
