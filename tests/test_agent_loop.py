"""Agent loop tests with a scripted model, no network."""

from collections.abc import Sequence
from pathlib import Path

from cabildo.agent.client import parse_reply
from cabildo.agent.loop import run
from cabildo.agent.tools import TOOLS
from cabildo.agent.types import Context, Endpoint, Message, Tool, ToolCall


def scripted(*replies: Message):
    it = iter(replies)

    def ask(_e: Endpoint, _m: Sequence[Message], _t: Sequence[Tool]) -> Message:
        return next(it)

    return ask


def call(name: str, **args: object) -> Message:
    return Message("assistant", calls=(ToolCall("c1", name, args),))


def test_edit_then_answer(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    out = run(
        "fix",
        endpoint=Endpoint(),
        tools=TOOLS,
        ctx=Context(cwd=tmp_path),
        ask=scripted(
            call("edit", path="a.txt", old="hello", new="bye"), Message("assistant", "done")
        ),
    )
    assert (tmp_path / "a.txt").read_text() == "bye"
    assert out[-1].text == "done"


def test_edit_refuses_ambiguous_match(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x x")
    out = run(
        "fix",
        endpoint=Endpoint(),
        tools=TOOLS,
        ctx=Context(cwd=tmp_path),
        ask=scripted(call("edit", path="a.txt", old="x", new="y"), Message("assistant", "ok")),
    )
    assert (tmp_path / "a.txt").read_text() == "x x"
    assert "matched 2 times" in out[3].text


def test_claimed_path_blocks_edit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")

    class Held:
        def acquire(self, path: Path) -> str | None:
            return "other-agent"

        def release_all(self) -> None:
            pass

    out = run(
        "fix",
        endpoint=Endpoint(),
        tools=TOOLS,
        ctx=Context(cwd=tmp_path, claims=Held()),
        ask=scripted(
            call("edit", path="a.txt", old="hello", new="bye"), Message("assistant", "ok")
        ),
    )
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert "claimed by other-agent" in out[3].text


def test_path_outside_cwd_is_refused(tmp_path: Path) -> None:
    out = run(
        "x",
        endpoint=Endpoint(),
        tools=TOOLS,
        ctx=Context(cwd=tmp_path),
        ask=scripted(call("read", path="../etc/passwd"), Message("assistant", "ok")),
    )
    assert "outside" in out[3].text


def test_gives_up_after_repeated_bad_calls(tmp_path: Path) -> None:
    bad = call("nope")
    out = run(
        "x",
        endpoint=Endpoint(),
        tools=TOOLS,
        ctx=Context(cwd=tmp_path),
        ask=scripted(bad, bad, bad, bad),
    )
    assert out[-1].text.startswith("stopped:")


def test_parse_reply_marks_bad_json() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"id": "1", "function": {"name": "read", "arguments": "{oops"}}],
                }
            }
        ]
    }
    assert "__invalid__" in parse_reply(body).calls[0].args
