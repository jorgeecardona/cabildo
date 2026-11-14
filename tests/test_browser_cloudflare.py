"""The Cloudflare handoff: the agent browser stops, the human passes the check, the cookie stays."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cabildo.browser import server
from cabildo.browser.server import Session, is_challenge, merge_cf_cookies


@pytest.mark.parametrize(
    ("title", "frames", "expected"),
    [
        ("Just a moment...", [], True),
        ("Just a moment…", [], True),
        ("Attention Required! | Cloudflare", [], True),
        ("Shop", ["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x"], True),
        ("Shop", ["https://shop.test/"], False),
        ("Moments of joy", [], False),
    ],
)
def test_is_challenge(title: str, frames: list[str], expected: bool) -> None:
    assert is_challenge(title, frames) is expected


def test_merge_keeps_logins_and_takes_only_cloudflare_cookies() -> None:
    state = {
        "cookies": [
            {"name": "session", "domain": "shop.test", "path": "/", "value": "login"},
            {"name": "cf_clearance", "domain": ".shop.test", "path": "/", "value": "old"},
        ],
        "origins": [{"origin": "https://shop.test"}],
    }
    live = [
        {"name": "cf_clearance", "domain": ".shop.test", "path": "/", "value": "new"},
        {"name": "tracking", "domain": "shop.test", "path": "/", "value": "agent-only"},
    ]
    out = merge_cf_cookies(state, live)
    values = {c["name"]: c["value"] for c in out["cookies"]}
    assert values == {"session": "login", "cf_clearance": "new"}
    assert out["origins"] == state["origins"]


class Frame:
    def __init__(self, url: str) -> None:
        self.url = url


class ChallengePage:
    """A page that shows the check until ``polls`` title reads have passed."""

    url = "https://shop.test/item"

    def __init__(self, polls: int) -> None:
        self.polls = polls

    @property
    def frames(self) -> list[Frame]:
        return [Frame(self.url)]

    async def title(self) -> str:
        self.polls -= 1
        return "Just a moment..." if self.polls >= 0 else "Shop"

    async def bring_to_front(self) -> None:
        return None


class Context:
    async def cookies(self) -> list[dict[str, Any]]:
        return [{"name": "cf_clearance", "domain": ".shop.test", "path": "/", "value": "earned"}]


def _session(tmp_path: Path, seconds: float) -> Session:
    s = Session(storage_state=tmp_path / "state.json", handoff_seconds=seconds)
    s._context = Context()  # type: ignore[assignment]
    return s


_real_sleep = asyncio.sleep


async def _no_sleep(_seconds: float) -> None:
    await _real_sleep(0)


def test_handoff_waits_for_the_human_then_saves_the_cookie(tmp_path, monkeypatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(server.desktop, "notify", lambda _s, b, **_k: alerts.append(b) or True)
    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep)
    session = _session(tmp_path, seconds=30)
    note = asyncio.run(server.handoff(session, ChallengePage(polls=3)))  # type: ignore[arg-type]
    assert "passed by the human" in note
    assert alerts
    assert "shop.test" in alerts[0]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["cookies"][0]["value"] == "earned"
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_handoff_gives_up_without_clicking(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server.desktop, "notify", lambda *_a, **_k: True)
    session = _session(tmp_path, seconds=0.05)
    note = asyncio.run(server.handoff(session, ChallengePage(polls=10**6)))  # type: ignore[arg-type]
    assert note.startswith("blocked by a Cloudflare check on shop.test")
    assert not (tmp_path / "state.json").exists()
