"""agents.py: `claude agents --json` parsing and the pane-title matcher."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from cabildo.workspace import agents
from cabildo.workspace.agents import AgentState


def agent(
    sid: str,
    name: str = "",
    kind: str = "background",
    status: str = "",
    state: str = "",
    pid: int | None = None,
) -> AgentState:
    return AgentState(
        session_id=sid,
        agent_id=sid[:8],
        kind=kind,
        name=name,
        cwd="/x",
        status=status,
        state=state,
        pid=pid,
    )


def test_live_agents_parses_claude_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {
            "id": "481c74f5",
            "sessionId": "481c74f5-full",
            "kind": "background",
            "name": "bravo",
            "cwd": "/p",
            "state": "done",
            "status": "idle",
            "pid": 42,
        },
        {"id": "zzz", "sessionId": "", "kind": "interactive"},  # no session id: skipped
        {"sessionId": "nopid-full", "kind": "interactive", "pid": "x"},  # bad pid -> None
        "not an object",  # skipped
        {"sessionId": "odd-full", "name": 7, "pid": True},  # bad types -> defaults
    ]
    seen: dict = {}

    def fake_run(argv, **kw) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, stdin=kw.get("stdin"))
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(agents.subprocess, "run", fake_run)
    got = agents.live_agents()
    assert got is not None
    assert seen == {"argv": ["claude", "agents", "--json", "--all"], "stdin": subprocess.DEVNULL}
    assert set(got) == {"481c74f5-full", "nopid-full", "odd-full"}
    assert (got["odd-full"].name, got["odd-full"].pid) == ("", None)
    b = got["481c74f5-full"]
    assert (b.agent_id, b.kind, b.name, b.cwd, b.state, b.pid) == (
        "481c74f5",
        "background",
        "bravo",
        "/p",
        "done",
        42,
    )
    n = got["nopid-full"]
    assert (n.agent_id, n.pid, n.name) == ("nopid-fu", None, "")


@pytest.mark.parametrize("error", [FileNotFoundError("claude"), subprocess.TimeoutExpired("c", 6)])
def test_live_agents_is_none_when_claude_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*_a, **_k):
        raise error

    monkeypatch.setattr(agents.subprocess, "run", fail)
    assert agents.live_agents() is None


@pytest.mark.parametrize(
    ("rc", "stdout"),
    [
        (1, "[]"),  # failed, whatever it printed
        (0, ""),  # nothing printed
        (0, "Warning: update available\n[]"),  # not JSON
        (0, '{"sessionId": "x"}'),  # not a list
    ],
)
def test_live_agents_is_none_for_unusable_output(
    monkeypatch: pytest.MonkeyPatch, rc: int, stdout: str
) -> None:
    monkeypatch.setattr(
        agents.subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], rc, stdout, "")
    )
    assert agents.live_agents() is None


def test_live_agents_with_none_running_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agents.subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, "[]", "")
    )
    assert agents.live_agents() == {}


def test_state_flags() -> None:
    assert agent("a", status="waiting").needs_input
    assert agent("a", state="blocked").needs_input
    assert agent("a", status="busy").working
    assert agent("a", pid=1).alive and not agent("a", state="done").alive
    assert agent("a").attachable and not agent("a", kind="interactive").attachable


AGENTS = {
    a.session_id: a
    for a in [
        agent("bravo-id", "bravo exact reply test"),
        agent("clear-1", "clear-conversation"),
        agent("clear-2", "clear-conversation"),
        agent("laptop-id", "My latop died here we were in the middle of something"),
    ]
}


@pytest.mark.parametrize(
    ("title", "current", "expected"),
    [
        ("bravo exact reply test", None, "bravo-id"),  # exact name
        ("✳ bravo exact reply test", "other", "bravo-id"),  # spinner glyph while working
        ("⠂ My latop died…", None, "laptop-id"),  # truncated title
        ("9 awaiting input · claude agents", "other", None),  # agent view: no session shown
        ("✳ Claude Code", "other", None),  # fresh session, no name yet
        ("earth", "other", None),  # tmux default (host name)
        ("clear-conversation", None, None),  # two agents share the name
        ("clear-conversation", "clear-2", "clear-2"),  # ...the current one is kept
        ("", "other", None),
    ],
)
def test_session_for_title(title: str, current: str | None, expected: str | None) -> None:
    assert agents.session_for_title(title, AGENTS, current) == expected


def test_remove_stops_then_rms(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_k) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(agents.subprocess, "run", fake_run)
    assert agents.remove("481c74f5") is None
    assert calls == [["claude", "stop", "481c74f5"], ["claude", "rm", "481c74f5"]]


def test_remove_returns_claudes_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **_k) -> subprocess.CompletedProcess[str]:
        rc = 1 if argv[1] == "rm" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="unpushed commits\nmore")

    monkeypatch.setattr(agents.subprocess, "run", fake_run)
    assert agents.remove("x") == "unpushed commits"


def test_wait_exit() -> None:
    p = subprocess.Popen(["true"])
    p.wait()  # exited and reaped
    assert agents.wait_exit([p.pid], timeout=1)
    assert not agents.wait_exit([os.getpid()], timeout=0.1)  # still running


def test_wait_exit_counts_a_zombie_as_gone() -> None:
    p = subprocess.Popen(["true"])  # exits; nobody reaps it yet
    assert agents.wait_exit([p.pid], timeout=2)
    p.wait()


def test_shared_snapshot_is_reused_while_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_live() -> dict[str, AgentState]:
        calls.append(1)
        return {"s1": agent("s1")}

    monkeypatch.setattr(agents, "live_agents", fake_live)
    first = agents.shared_live_agents(not_before=time.time())
    again = agents.shared_live_agents(not_before=first.taken_at)  # another instance, in time
    assert len(calls) == 1
    assert again == first
    agents.shared_live_agents(not_before=time.time() + 1)  # an event: needs a newer one
    assert len(calls) == 2


def test_failed_call_is_not_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "live_agents", lambda: None)
    assert agents.shared_live_agents(not_before=0.0).agents is None
    monkeypatch.setattr(agents, "live_agents", lambda: {"s1": agent("s1")})
    assert agents.shared_live_agents(not_before=0.0).agents == {"s1": agent("s1")}
