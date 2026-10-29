"""`cabildo browser login`: free the profile, keep the last good state, write it private."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from cabildo import procs
from cabildo.browser import fleet, login

PROFILE = Path("/profiles/main")
CHROME = f"/opt/google/chrome/chrome --remote-debugging-pipe --user-data-dir={PROFILE}"
OTHER = "/opt/google/chrome/chrome --remote-debugging-pipe --user-data-dir=/profiles/other"
RENDERER = "/opt/google/chrome/chrome --type=renderer --remote-debugging-pipe"


def proc(pid: int, ppid: int, cmdline: str, rss_kb: int = 1024) -> procs.Proc:
    return procs.Proc(
        pid=pid, ppid=ppid, comm="chrome", rss_kb=rss_kb, started_at=0.0, cmdline=cmdline
    )


def test_collect_returns_the_last_state_read_before_the_browser_closed() -> None:
    reads: list[dict[str, Any] | None] = [{"cookies": [1]}, {"cookies": [1, 2]}, None]
    slept: list[float] = []
    got = login.collect(lambda: reads.pop(0), sleep=slept.append, poll=0.5)
    assert got == {"cookies": [1, 2]}
    assert slept == [0.5, 0.5]


def test_collect_returns_none_when_the_browser_was_never_readable() -> None:
    assert login.collect(lambda: None, sleep=lambda _s: None) is None


def test_write_state_is_mode_600_and_counts(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "state.json"
    saved = login.write_state(target, {"cookies": [{}, {}], "origins": [{}]})
    assert (saved.cookies, saved.origins) == (2, 1)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text())["cookies"] == [{}, {}]


def test_write_state_replaces_a_looser_existing_file_with_mode_600(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    target.chmod(0o644)
    login.write_state(target, {"cookies": [], "origins": []})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_on_profile_finds_the_browser_on_that_profile_with_its_helpers() -> None:
    processes = [
        proc(10, 1, CHROME, rss_kb=100),
        proc(11, 10, RENDERER, rss_kb=50),
        proc(20, 1, OTHER),
    ]
    (target,) = fleet.on_profile(processes, PROFILE)
    assert target.pids == (10, 11)
    assert target.rss_kb == 150


def test_free_profile_stops_that_browser_and_removes_the_locks(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).symlink_to("/nowhere")
    chrome = f"/opt/google/chrome/chrome --remote-debugging-pipe --user-data-dir={profile}"
    stopped: list[tuple[fleet.Target, ...]] = []

    def fake_stop(targets) -> fleet.Stopped:
        stopped.append(tuple(targets))
        return fleet.Stopped(targets=tuple(targets), gone=(10,), left=())

    done = login.free_profile(profile, [proc(10, 1, chrome)], stop=fake_stop)
    assert [t.pids for t in stopped[0]] == [(10,)]
    assert done.gone == (10,)
    assert list(profile.iterdir()) == []


def test_default_profile_keeps_the_earlier_profile_when_it_exists(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert login.default_profile() == tmp_path / ".local/share/cabildo/browser-profile"
    old = tmp_path / ".cache/ms-playwright-mcp/mcp-chrome-for-testing-3b855a0"
    old.mkdir(parents=True)
    assert login.default_profile() == old
