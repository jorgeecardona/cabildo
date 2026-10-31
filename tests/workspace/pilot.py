"""Drive the real app headless (Textual's pilot) with `claude agents` faked."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from lines import PROJECT
from textual.pilot import Pilot

from cabildo.workspace import agents
from cabildo.workspace.agents import AgentState
from cabildo.workspace.app import SwitcherApp

Scenario = Callable[[SwitcherApp, Pilot], Awaitable[None]]


async def settle(app: SwitcherApp, pilot: Pilot) -> None:
    """Let pending messages and worker threads finish, also the workers they start
    (a poll asked for while one ran starts after it).
    """
    for _ in range(50):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not app.workers:  # finished workers leave the manager
            return


async def wait_until(pilot: Pilot, check: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        await pilot.pause(0.05)
    return check()


def drive(
    monkeypatch: pytest.MonkeyPatch,
    scenario: Scenario,
    *,
    live: dict[str, AgentState] | None = None,
    size: tuple[int, int] = (120, 30),
    cwd: str = PROJECT,
    **app_args: Any,
) -> SwitcherApp:
    """Run the app, settle, run the scenario; the app is closed afterwards. `app_args`:
    what the command line passes (all_dirs, only).
    """
    monkeypatch.setattr(agents, "live_agents", lambda: dict(live or {}))
    app = SwitcherApp(cwd, **app_args)

    async def go() -> None:
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            await scenario(app, pilot)

    asyncio.run(go())
    return app


def background(sid: str, name: str = "n") -> AgentState:
    return AgentState(
        session_id=sid,
        agent_id=sid[:8],
        kind="background",
        name=name,
        cwd="/x",
        status="idle",
        state="done",
        pid=None,
    )
