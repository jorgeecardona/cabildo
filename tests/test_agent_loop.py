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


def test_history_continues_a_conversation(tmp_path: Path) -> None:
    seen: list[int] = []

    def ask(_e: Endpoint, messages: Sequence[Message], _t: Sequence[Tool]) -> Message:
        seen.append(len(messages))
        return Message("assistant", "ok")

    kw = {"endpoint": Endpoint(), "tools": TOOLS, "ctx": Context(cwd=tmp_path), "ask": ask}
    first = run("one", **kw)
    run("two", history=first, **kw)
    assert seen == [2, 4]  # system+user, then system+user+assistant+user


def test_assemble_stream_joins_text_and_tool_call_pieces() -> None:
    from cabildo.agent.client import assemble_stream

    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read","arguments":"{\\"pa"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\\":\\"a\\"}"}}]}}]}',
        'data: {"choices":[]}',
        "data: [DONE]",
    ]
    pieces: list[str] = []
    msg = assemble_stream(lines, pieces.append)
    assert pieces == ["Hel", "lo"]
    assert msg.text == "Hello"
    assert msg.calls[0].name == "read"
    assert dict(msg.calls[0].args) == {"path": "a"}


def test_on_tool_sees_each_result(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    seen: list[tuple[str, str]] = []
    run(
        "x",
        endpoint=Endpoint(),
        tools=TOOLS,
        ctx=Context(cwd=tmp_path),
        on_tool=lambda c, r: seen.append((c.name, r)),
        ask=scripted(call("read", path="a.txt"), Message("assistant", "ok")),
    )
    assert seen == [("read", "hi")]
