"""The mode line shows the gateway upstream that serves the session."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from cabildo import gateway, gateway_usage, usage
from cabildo.workspace import modeline, plan, upstreams


def status(**over: object) -> dict:
    row = {
        "name": "anthropic",
        "kind": "passthrough",
        "ready": True,
        "set_aside_s": 0,
        "reason": "",
        "requests": 412,
        "failovers": 37,
        "last_error": "",
        "forced": False,
    }
    return {**row, **over}


REPLY = {
    "upstreams": [
        status(ready=False, set_aside_s=3 * 86400, reason="rate or usage limit"),
        status(name="bedrock", kind="bedrock-invoke", requests=38, failovers=0, forced=True),
    ],
    "sessions": {"s1": "bedrock"},
}


def test_the_view_names_the_upstream_of_a_session() -> None:
    view = upstreams.parse(REPLY)
    assert view is not None
    found = view.serving(("other", "s1"))
    assert found is not None and found.name == "bedrock"
    assert view.serving(("other",)) is None
    assert modeline.fmt_upstream(found) == "upstream      bedrock"


def test_the_gateway_table_has_one_row_per_upstream_and_the_reason() -> None:
    view = upstreams.parse(REPLY)
    assert view is not None
    lines = modeline.fmt_gateway(view, "bedrock")
    assert [x.split() for x, _style in lines] == [
        ["gateway", "requests", "failovers"],
        ["anthropic", "412", "37", "set", "aside", "3d"],
        ["rate", "or", "usage", "limit"],
        ["bedrock", "38", "0", "ready,", "first"],
    ]
    assert [style for _x, style in lines] == ["", "", "dim", "bold"]


def test_an_upstream_that_served_replies_shows_what_it_used() -> None:
    view = upstreams.parse(REPLY)
    assert view is not None
    day = usage.Window(input=100, cost=1.5)
    view = replace(view, spent=(gateway_usage.Spent("bedrock", day, day, day),))
    lines = [x.strip() for x, _style in modeline.fmt_gateway(view)]
    assert lines[-1] == "today $1.50 · 7d $1.50"
    assert lines[-2].startswith("bedrock")


def test_a_reply_without_sessions_or_with_another_shape() -> None:
    old = upstreams.parse({"upstreams": REPLY["upstreams"]})
    assert old is not None and old.serving(("s1",)) is None
    assert upstreams.parse({"upstreams": [{"name": "x"}]}) is None
    assert upstreams.parse({}) is None


def test_a_used_up_limit_shows_no_pace_and_no_eta() -> None:
    usage = plan.PlanUsage("team", plan.Limit(49, 0), plan.Limit(100, 0))
    pace = plan.PlanPace(
        plan.Pace(19.9, 49, 0, 1_000.0, True), plan.Pace(1.1, 100, 0, 1_000.0, True)
    )
    _head, five, seven = modeline.fmt_plan(usage, pace)
    assert "19.9%/h" in five
    assert seven.split() == ["7d", "100%"]


def test_claude_uses_the_gateway_only_when_its_settings_point_at_that_port(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert not gateway.claude_uses(path, 8484)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8484"}}))
    assert gateway.claude_uses(path, 8484)
    assert not gateway.claude_uses(path, 9999)
    path.write_text("{")
    assert not gateway.claude_uses(path, 8484)
