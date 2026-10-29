"""`cabildo browser status` and `reap`: finding browser processes and choosing what to stop."""

from __future__ import annotations

import signal

from cabildo import procs
from cabildo.browser import fleet
from cabildo.browser.fleet import Target

SERVER = "node /x/node_modules/@playwright/mcp/cli.js --browser chrome --isolated"
CHROME = "/opt/google/chrome/chrome --remote-debugging-pipe --user-data-dir=/profiles/pw1"
RENDERER = "/opt/google/chrome/chrome --type=renderer --remote-debugging-pipe"


def proc(pid: int, ppid: int, comm: str, cmdline: str = "", rss_kb: int = 1024) -> procs.Proc:
    return procs.Proc(
        pid=pid, ppid=ppid, comm=comm, rss_kb=rss_kb, started_at=1000.0, cmdline=cmdline or comm
    )


def live_session() -> list[procs.Proc]:
    """A claude (100) with a server (200), its chrome (300) and a renderer (301)."""
    return [
        proc(1, 0, "systemd"),
        proc(100, 1, "claude"),
        proc(200, 100, "node", SERVER, rss_kb=4096),
        proc(300, 200, "chrome", CHROME, rss_kb=102400),
        proc(301, 300, "chrome", RENDERER, rss_kb=51200),
    ]


def find(processes: list[procs.Proc]) -> fleet.Fleet:
    return fleet.find(processes, owned=lambda _pid: True)


def test_a_server_owns_the_chrome_processes_under_it() -> None:
    got = find(live_session())
    (server,) = got.servers
    assert server.pid == 200
    assert server.pids == (200, 300, 301)
    assert server.chrome_count == 2
    assert server.rss_kb == 4096 + 102400 + 51200
    assert server.session_alive
    assert server.flags.isolated
    assert got.strays == ()


def test_a_server_whose_claude_is_gone_has_lost_its_session() -> None:
    processes = [p for p in live_session() if p.pid != 100]
    processes[1] = proc(200, 1, "node", SERVER)  # adopted by init
    (server,) = find(processes).servers
    assert not server.session_alive


def test_a_server_adopted_by_a_systemd_subreaper_has_lost_its_session() -> None:
    processes = live_session()
    processes[1] = proc(100, 1, "systemd")  # the claude was replaced by a user manager
    (server,) = find(processes).servers
    assert not server.session_alive


def test_a_shell_that_only_mentions_the_server_is_not_a_server() -> None:
    shell = proc(500, 100, "bash", "bash -c grep @playwright/mcp/cli.js")
    assert not fleet.is_server(shell)
    assert find([*live_session(), shell]).servers[0].pid == 200


def test_the_cabildo_server_is_found_too() -> None:
    cabildo = proc(600, 100, "cabildo", "/venv/bin/cabildo browser serve --isolated")
    python = proc(601, 100, "python", "/venv/bin/python -m cabildo.browser.server")
    assert fleet.is_server(cabildo)
    assert fleet.is_server(python)
    assert not fleet.is_server(proc(602, 100, "cabildo", "/venv/bin/cabildo browser status"))


def test_a_chrome_with_no_server_and_no_live_parent_is_a_stray() -> None:
    processes = [
        proc(1, 0, "systemd"),
        proc(300, 1, "chrome", CHROME, rss_kb=100),
        proc(301, 300, "chrome", RENDERER, rss_kb=50),
    ]
    (stray,) = find(processes).strays
    assert stray.pid == 300
    assert stray.pids == (300, 301)
    assert stray.rss_kb == 150
    assert stray.user_data_dir == "/profiles/pw1"


def test_a_chrome_started_by_a_live_process_is_not_a_stray() -> None:
    processes = [proc(1, 0, "systemd"), proc(50, 1, "pytest"), proc(300, 50, "chrome", CHROME)]
    assert find(processes).strays == ()


def test_processes_of_another_user_are_ignored() -> None:
    got = fleet.find(live_session(), owned=lambda pid: pid != 200)
    assert got.servers == ()


def test_ancestors_include_the_pid_itself_and_stop_at_init() -> None:
    assert fleet.ancestors(300, live_session()) == frozenset({300, 200, 100})


def test_reap_by_default_picks_only_what_lost_its_session() -> None:
    live = find(live_session())
    assert fleet.pick(live) == ()
    gone = [p for p in live_session() if p.pid != 100]
    gone[1] = proc(200, 1, "node", SERVER, rss_kb=4096)
    (target,) = fleet.pick(find(gone))
    assert target.pids == (200, 300, 301)
    assert "session gone" in target.label


def test_reap_by_default_also_picks_strays() -> None:
    processes = [proc(1, 0, "systemd"), proc(300, 1, "chrome", CHROME)]
    (target,) = fleet.pick(find(processes))
    assert target.pids == (300,)
    assert "stray" in target.label


def test_everything_picks_live_servers_but_not_the_one_of_this_session() -> None:
    got = find(live_session())
    other = [
        *live_session(),
        proc(110, 1, "claude"),
        proc(210, 110, "node", SERVER),
    ]
    mine = frozenset({100})
    picked = fleet.pick(find(other), everything=True, protect=mine)
    assert [t.pids[0] for t in picked] == [210]
    assert [t.pids[0] for t in fleet.pick(got, everything=True, protect=mine)] == []


def test_include_mine_picks_the_server_of_this_session_too() -> None:
    picked = fleet.pick(
        find(live_session()), everything=True, include_mine=True, protect=frozenset({100})
    )
    assert [t.pids[0] for t in picked] == [200]


def test_a_tree_holding_a_protected_pid_is_never_picked() -> None:
    processes = [proc(1, 0, "systemd"), proc(300, 1, "chrome", CHROME), proc(301, 300, "bash")]
    assert fleet.pick(find(processes), protect=frozenset({301})) == ()


class FakeOS:
    """A process table where TERM kills the cooperative pids and KILL kills the rest."""

    def __init__(self, alive: set[int], stubborn: set[int]) -> None:
        self.alive_pids = alive
        self.stubborn = stubborn
        self.sent: list[tuple[int, int]] = []

    def kill(self, pid: int, sig: int) -> None:
        self.sent.append((pid, sig))
        if pid not in self.alive_pids:
            raise ProcessLookupError
        if sig == signal.SIGKILL or pid not in self.stubborn:
            self.alive_pids.discard(pid)

    def alive(self, pid: int) -> bool:
        return pid in self.alive_pids


def test_stop_sends_term_and_leaves_no_kill_when_everything_exits() -> None:
    fake = FakeOS({1, 2, 3}, stubborn=set())
    done = fleet.stop(
        [Target("t", (1, 2, 3), 3072)], kill=fake.kill, alive=fake.alive, sleep=lambda _s: None
    )
    assert {sig for _pid, sig in fake.sent} == {signal.SIGTERM}
    assert done.gone == (1, 2, 3)
    assert done.left == ()
    assert done.freed_kb == 3072


def test_stop_kills_what_ignores_term() -> None:
    fake = FakeOS({1, 2}, stubborn={2})
    done = fleet.stop(
        [Target("t", (1, 2), 2048)], kill=fake.kill, alive=fake.alive, sleep=lambda _s: None
    )
    assert (2, signal.SIGKILL) in fake.sent
    assert (1, signal.SIGKILL) not in fake.sent
    assert done.left == ()


def test_stop_reports_a_pid_that_survives_kill() -> None:
    class Immortal(FakeOS):
        def kill(self, pid: int, sig: int) -> None:
            self.sent.append((pid, sig))

    fake = Immortal({1}, stubborn={1})
    done = fleet.stop(
        [Target("t", (1,), 1024)], kill=fake.kill, alive=fake.alive, sleep=lambda _s: None
    )
    assert done.left == (1,)
    assert done.freed_kb == 0


def test_stop_ignores_a_pid_that_already_exited() -> None:
    fake = FakeOS(set(), stubborn=set())
    done = fleet.stop(
        [Target("t", (9,), 1)], kill=fake.kill, alive=fake.alive, sleep=lambda _s: None
    )
    assert done.left == ()
