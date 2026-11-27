"""idle.py: which Claudes are stopped for being idle, and what the app does then."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from lines import assistant, user
from pilot import drive, settle, wait_until
from textual.widgets import DataTable

from cabildo import config, crons, store
from cabildo.workspace import attach, idle
from cabildo.workspace.idle import Candidate, Process

HOUR = 3600.0
NOW = time.time()


def report(claude_home: Path, pid: int, sid: str, status: str = "idle", ago: float = 7) -> None:
    """What Claude Code writes about a running session; ``ago`` in hours."""
    folder = claude_home / "sessions"
    folder.mkdir(exist_ok=True)
    (folder / f"{pid}.json").write_text(
        json.dumps(
            {
                "sessionId": sid,
                "kind": "interactive",
                "status": status,
                "statusUpdatedAt": int((time.time() - ago * HOUR) * 1000),
            }
        )
    )


@pytest.fixture
def candidate(transcript) -> Candidate:
    path = transcript("s1", user("q"), mtime=NOW - 8 * HOUR)
    return Candidate("s1", 100, ("s1",), path)


def stoppable(c: Candidate, *processes: Process, scheduled: bool = False) -> bool:
    found = idle.stoppable(
        [c],
        time.time() - 6 * HOUR,
        list_processes=lambda: processes,
        has_schedule=lambda _ids: scheduled,
    )
    return found == (c,)


def test_an_idle_claude_is_stoppable(claude_home, candidate) -> None:
    report(claude_home, 100, "s1")
    os.utime(candidate.transcript)  # Claude Code appends to it while idle
    mcp = Process(101, 100, "python -m cabildo mcp")
    assert stoppable(candidate, mcp)


@pytest.mark.parametrize(
    ("sid", "status", "ago"),
    [("s1", "busy", 7), ("s1", "waiting", 7), ("s1", "idle", 5), ("other", "idle", 7)],
)
def test_not_idle_long_enough_is_kept(claude_home, candidate, sid, status, ago) -> None:
    report(claude_home, 100, sid, status, ago)
    assert not stoppable(candidate)


def test_no_report_is_kept(candidate) -> None:
    assert not stoppable(candidate)


def test_a_subagent_that_wrote_lately_keeps_it(claude_home, candidate) -> None:
    report(claude_home, 100, "s1")
    agent = candidate.transcript.with_suffix("") / "subagents" / "workflows" / "agent-1.jsonl"
    agent.parent.mkdir(parents=True)
    agent.write_text("{}\n")
    assert not stoppable(candidate)
    os.utime(agent, (NOW - 8 * HOUR, NOW - 8 * HOUR))
    assert stoppable(candidate)


def test_a_shell_command_or_a_schedule_keeps_it(claude_home, candidate) -> None:
    report(claude_home, 100, "s1")
    shell = "/bin/bash -c source /home/u/.claude/shell-snapshots/snapshot-bash-1.sh && make"
    assert not stoppable(candidate, Process(101, 100, "node x"), Process(102, 101, shell))
    assert stoppable(candidate, Process(102, 999, shell))  # another Claude's
    assert not stoppable(candidate, scheduled=True)


def test_ps_failing_keeps_it(claude_home, candidate) -> None:
    report(claude_home, 100, "s1")
    assert idle.stoppable([candidate], time.time() - HOUR, list_processes=lambda: None) == ()


def test_processes_lists_this_one() -> None:
    found = idle.processes()
    assert found is not None
    assert any(p.pid == os.getpid() and p.ppid == os.getppid() for p in found)


def test_scheduled_reads_the_crons_of_the_conversation() -> None:
    assert not idle.scheduled(("s1", "s2"))
    conn = store.connect()
    crons.add_wakeup(conn, session_id="s2", delay_s=600, prompt="check")
    conn.close()
    assert idle.scheduled(("s1", "s2"))


@pytest.mark.parametrize(
    ("text", "hours"),
    [("", 6), ("stop_idle_hours = 0", 0), ("stop_idle_hours = 1.5", 1.5),
     ("stop_idle_hours = -1", 6), ('stop_idle_hours = "2"', 6), ("stop_idle_hours = true", 6)],
)  # fmt: skip
def test_the_setting(bus_home: Path, tmp_path: Path, text: str, hours: float) -> None:
    bus_home.mkdir(exist_ok=True)
    (bus_home / "config.toml").write_text(f"[workspace]\n{text}\n")
    assert config.stop_idle_hours(str(tmp_path)) == hours


def test_the_look_survives_a_reload() -> None:
    pane = attach.Handed(5, 6, "pane-1", "s1", "/w", "", "t", 24, 80, seen_at=12.5)
    assert attach.decode(attach.encode([pane])) == [pane]


# --- the app -------------------------------------------------------------------


@pytest.fixture
def two_rows(transcript) -> None:
    for sid, ago in (("s1", 8), ("s2", 9)):
        transcript(sid, user(f"q {sid}"), assistant("a"), mtime=NOW - ago * HOUR)


def test_the_app_stops_a_hidden_idle_claude_and_enter_resumes_it(
    two_rows, fake_claude, claude_home, monkeypatch
) -> None:
    async def scenario(app, pilot) -> None:
        by_id = {r.head.session_id: r for r in app.rows}
        for sid in ("s1", "s2"):
            await app._open(by_id[sid])
        panes = {p.session_id: p for p in app._panes()}
        assert await wait_until(pilot, lambda: all(p.pid for p in panes.values()))
        for sid, pane in panes.items():
            report(claude_home, pane.pid, sid)

        app._idle_tick()  # both were looked at just now
        await settle(app, pilot)
        assert app._open_ids() == {"s1", "s2"}

        for pane in panes.values():
            pane.seen_at = time.time() - 7 * HOUR
        app._idle_tick()
        assert await wait_until(pilot, lambda: len(app._panes()) == 1)
        await settle(app, pilot)
        assert app.shown_session() == "s2"  # the shown one stays
        assert app.was_open == ("s1",)
        assert app._mark(by_id["s1"], "s2", app._open_ids()) == "○"

        app.query_one(DataTable).move_cursor(row=app.rows.index(by_id["s1"]))
        await app.action_resume()
        assert app.shown_session() == "s1" and app.was_open == ()

    drive(monkeypatch, scenario)
