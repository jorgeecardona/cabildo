"""The escalate tool, with a fake model: no network, no cost."""

from pathlib import Path

from cabildo.agent.escalate import Escalator, parse_text
from cabildo.agent.loop import run
from cabildo.agent.tools import TOOLS
from cabildo.agent.types import Context, Endpoint, Message, ToolCall


def test_parse_text_joins_text_blocks_and_skips_others() -> None:
    body = {
        "content": [
            {"type": "text", "text": "a"},
            {"type": "thinking"},
            {"type": "text", "text": "b"},
        ]
    }
    assert parse_text(body) == "ab"


def test_limit_stops_further_questions(tmp_path: Path) -> None:
    seen: list[str] = []
    esc = Escalator(lambda q: seen.append(q) or "answer", limit=1)
    ctx = Context(cwd=tmp_path)
    assert esc.run({"question": "one"}, ctx) == "answer"
    assert esc.run({"question": "two"}, ctx).startswith("error: escalation limit")
    assert seen == ["one"]


def test_a_failing_api_becomes_an_error_message(tmp_path: Path) -> None:
    def boom(_q: str) -> str:
        raise OSError("no network")

    assert "no network" in Escalator(boom, limit=1).run({"question": "q"}, Context(cwd=tmp_path))


def test_loop_routes_escalate_calls_to_the_tool(tmp_path: Path) -> None:
    esc = Escalator(lambda _q: "use a dict", limit=3)
    replies = iter(
        [
            Message("assistant", calls=(ToolCall("c1", "escalate", {"question": "design?"}),)),
            Message("assistant", "done"),
        ]
    )
    out = run(
        "x",
        endpoint=Endpoint(),
        tools=(*TOOLS, esc.tool()),
        ctx=Context(cwd=tmp_path),
        ask=lambda _e, _m, _t: next(replies),
    )
    assert out[3].text == "use a dict"
