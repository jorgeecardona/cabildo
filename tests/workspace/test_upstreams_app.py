"""The running app shows the gateway rows in the mode line."""

from __future__ import annotations

from pilot import drive, wait_until
from textual.pilot import Pilot

from cabildo import gateway
from cabildo.workspace import upstreams
from cabildo.workspace.app import SwitcherApp


def test_the_mode_line_shows_the_gateway_table(monkeypatch) -> None:
    row = gateway.UpstreamStatus("bedrock", "bedrock-invoke", True, 0, "", 38, 0, "", False)
    monkeypatch.setattr(upstreams.Reader, "read", lambda _self: upstreams.View((row,)))
    seen: list[str] = []

    async def scenario(app: SwitcherApp, pilot: Pilot) -> None:
        def text() -> str:
            return app._modeline_text.plain if app._modeline_text else ""

        await wait_until(pilot, lambda: "bedrock" in text())
        seen.append(text())

    drive(monkeypatch, scenario)
    assert "gateway" in seen[0] and "failovers" in seen[0] and "38" in seen[0]
