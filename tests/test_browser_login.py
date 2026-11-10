"""`cabildo browser login`: free the profile, keep the last good state, write it private."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from cabildo import procs
from cabildo.browser import fleet, login

PROFILE = Path("/profiles/main")
CHROME = f"/opt/google/chrome/chrome --remote-debugging-pipe --user-data-dir={PROFILE}"
CHROME_ON = "/opt/google/chrome/chrome --remote-debugging-pipe --user-data-dir="
OTHER = "/opt/google/chrome/chrome --remote-debugging-pipe --user-data-dir=/profiles/other"
RENDERER = "/opt/google/chrome/chrome --type=renderer --remote-debugging-pipe"


def proc(pid: int, ppid: int, cmdline: str, rss_kb: int = 1024) -> procs.Proc:
    return procs.Proc(
        pid=pid, ppid=ppid, comm="chrome", rss_kb=rss_kb, started_at=0.0, cmdline=cmdline
    )


def test_plain_command_has_no_automation_flag(tmp_path: Path) -> None:
    cmd = login.plain_command("/usr/bin/google-chrome", tmp_path)
    assert cmd[0] == "/usr/bin/google-chrome"
    assert f"--user-data-dir={tmp_path}" in cmd
    assert not any("automation" in a or "remote-debugging" in a for a in cmd)


def test_browser_binary_maps_the_channel_to_the_first_installed_executable() -> None:
    found = {"google-chrome-stable": "/usr/bin/google-chrome-stable"}
    assert login.browser_binary("chrome", which=found.get) == "/usr/bin/google-chrome-stable"
    assert login.browser_binary("msedge", which=found.get) is None
    assert login.browser_binary("/opt/x/chrome", which=lambda n: n) == "/opt/x/chrome"


def test_run_stops_when_the_channel_has_no_executable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(procs, "processes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        login, "free_profile", lambda *_a, **_k: fleet.Stopped(targets=(), gone=(), left=())
    )
    monkeypatch.setattr(login, "browser_binary", lambda _c: None)
    said: list[str] = []
    assert login.run(tmp_path / "p", tmp_path / "s.json", channel="nope", say=said.append) is None
    assert "no executable" in said[-1]


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


def test_free_profile_keeps_the_locks_when_a_browser_survived(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("/nowhere")

    def survivor(targets) -> fleet.Stopped:
        return fleet.Stopped(targets=tuple(targets), gone=(), left=(10,))

    done = login.free_profile(profile, [proc(10, 1, f"{CHROME_ON}{profile}")], stop=survivor)
    assert done.left == (10,)
    assert (profile / "SingletonLock").is_symlink()


def test_run_does_not_open_a_profile_it_could_not_free(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(procs, "processes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        login, "free_profile", lambda *_a, **_k: fleet.Stopped(targets=(), gone=(), left=(7,))
    )
    said: list[str] = []
    assert login.run(tmp_path / "p", tmp_path / "s.json", channel="chrome", say=said.append) is None
    assert "not opening it" in said[-1]
    assert not (tmp_path / "s.json").exists()


def test_write_state_leaves_no_temporary_file_and_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"cookies": [1]}')
    target.chmod(0o644)
    login.write_state(target, {"cookies": [], "origins": []})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
