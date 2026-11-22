"""mcp_stdio.py: the stdio MCP server that replaces the ``mcp`` SDK."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pytest

from cabildo import mcp_stdio as m


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --- schemas ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hint", "schema"),
    [
        (str, {"type": "string"}),
        (int, {"type": "integer"}),
        (float, {"type": "number"}),
        (bool, {"type": "boolean"}),
        (Any, {}),
        (str | None, {"anyOf": [{"type": "string"}, {"type": "null"}]}),
        (list[str], {"type": "array", "items": {"type": "string"}}),
        (
            list[int] | None,
            {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "null"}]},
        ),
        (dict[str, Any], {"type": "object", "additionalProperties": True}),
        (dict[str, str], {"type": "object", "additionalProperties": {"type": "string"}}),
        (Literal["a", "b"], {"enum": ["a", "b"], "type": "string"}),
    ],
)
def test_a_type_hint_becomes_a_json_schema(hint: Any, schema: dict) -> None:
    assert m.schema_of(hint) == schema


def test_a_type_that_is_not_mapped_fails_when_the_tool_is_defined() -> None:
    server = m.McpServer("t")
    with pytest.raises(TypeError, match="unsupported"):

        @server.tool()
        def bad(x: set[int]) -> str:
            return ""

    with pytest.raises(TypeError, match="no type hint"):

        @server.tool()
        def untyped(x) -> str:  # type: ignore[no-untyped-def]
            return ""


def test_the_input_schema_lists_required_parameters_and_defaults() -> None:
    server = m.McpServer("t")

    @server.tool()
    def f(a: str, b: int = 3, c: str | None = None) -> str:
        """Do f."""
        return a

    (tool,) = run(server.list_tools())
    assert tool.description == "Do f."
    assert tool.input_schema == {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer", "default": 3},
            "c": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
        "required": ["a"],
    }
    assert f("x") == "x", "the decorator returns the function unchanged"


# --- coercion -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hint", "value", "expected"),
    [
        (int, 5, 5),
        (int, "5", 5),
        (int, 5.0, 5),
        (float, 2, 2.0),
        (float, "2.5", 2.5),
        (bool, "true", True),
        (bool, False, False),
        (list[str], ["a"], ["a"]),
        (list[int], "[1, 2]", [1, 2]),
        (dict[str, Any], '{"a": 1}', {"a": 1}),
        (str | None, None, None),
        (int | None, "7", 7),
        (Literal["png", "jpeg"], "png", "png"),
        (Any, object, object),
    ],
)
def test_loose_json_from_a_model_is_accepted(hint: Any, value: Any, expected: Any) -> None:
    assert m.coerce(hint, value) == expected


@pytest.mark.parametrize(
    ("hint", "value"),
    [
        (int, True),
        (int, 5.5),
        (int, "five"),
        (str, 5),
        (bool, "yes"),
        (list[str], "not json"),
        (list[str], [1]),
        (Literal["a"], "b"),
        (Literal[1], True),
        (float, True),
    ],
)
def test_a_value_that_does_not_fit_is_refused(hint: Any, value: Any) -> None:
    with pytest.raises(ValueError):
        m.coerce(hint, value)


# --- the protocol -------------------------------------------------------------------------------


def server_with_tools() -> m.McpServer:
    server = m.McpServer("demo", instructions="be kind")

    @server.tool()
    def add(a: int, b: int = 1) -> dict[str, int]:
        """Add."""
        return {"sum": a + b}

    @server.tool()
    async def shout(text: str) -> str:
        return text.upper()

    @server.tool(name="renamed")
    def original() -> None:
        return None

    @server.tool()
    def boom() -> str:
        msg = "it broke"
        raise RuntimeError(msg)

    @server.tool()
    def picture() -> m.Image:
        return m.Image(b"\x89PNG", "png")

    return server


def rpc(server: m.McpServer, method: str, params: Any = None, id_: Any = 1) -> dict | None:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return run(server.handle(msg))


def test_initialize_answers_the_version_the_client_asked_for_when_it_is_supported() -> None:
    server = server_with_tools()
    out = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert out is not None
    result = out["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "demo"
    assert result["instructions"] == "be kind"


def test_initialize_offers_the_latest_version_when_the_client_asks_for_an_unknown_one() -> None:
    out = rpc(server_with_tools(), "initialize", {"protocolVersion": "1900-01-01"})
    assert out is not None and out["result"]["protocolVersion"] == m.SUPPORTED_VERSIONS[0]


def test_ping_and_notifications() -> None:
    server = server_with_tools()
    assert rpc(server, "ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert rpc(server, "notifications/initialized", id_=None) is None


def test_tools_list_names_every_tool_in_order() -> None:
    out = rpc(server_with_tools(), "tools/list")
    assert out is not None
    names = [t["name"] for t in out["result"]["tools"]]
    assert names == ["add", "shout", "renamed", "boom", "picture"]
    json.dumps(out)  # the whole listing is JSON


def call(server: m.McpServer, name: str, arguments: Any = None) -> dict:
    out = rpc(server, "tools/call", {"name": name, "arguments": arguments})
    assert out is not None
    return out


def test_a_tool_call_returns_its_value_as_one_line_of_json() -> None:
    out = call(server_with_tools(), "add", {"a": 2, "b": "3"})
    assert out["result"] == {"content": [{"text": '{"sum":5}', "type": "text"}], "isError": False}


def test_an_async_tool_is_awaited_and_a_string_is_returned_as_it_is() -> None:
    out = call(server_with_tools(), "shout", {"text": "hi"})
    assert out["result"]["content"][0]["text"] == "HI"


def test_a_tool_that_returns_nothing_has_no_content() -> None:
    out = call(server_with_tools(), "renamed")
    assert out["result"] == {"content": [], "isError": False}


def test_an_image_becomes_an_image_block() -> None:
    block = call(server_with_tools(), "picture")["result"]["content"][0]
    assert block["type"] == "image" and block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == b"\x89PNG"


def test_a_tool_that_raises_is_an_error_result_not_a_protocol_error() -> None:
    result = call(server_with_tools(), "boom")["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Error executing tool boom: it broke"


def test_bad_arguments_are_an_error_result_that_names_each_problem() -> None:
    result = call(server_with_tools(), "add", {"a": "x"})["result"]
    assert result["isError"] is True
    assert "a: expected an integer, got str" in result["content"][0]["text"]
    missing = call(server_with_tools(), "add", {})["result"]
    assert "a: required" in missing["content"][0]["text"]


def test_an_unknown_tool_and_an_unknown_method_are_protocol_errors() -> None:
    unknown = call(server_with_tools(), "nope")
    assert unknown["error"]["code"] == m.INVALID_PARAMS and "nope" in unknown["error"]["message"]
    out = rpc(server_with_tools(), "server/discover", {})
    assert out is not None and out["error"]["code"] == m.METHOD_NOT_FOUND
    other = rpc(server_with_tools(), "prompts/list")
    assert other is not None and other["error"]["code"] == m.METHOD_NOT_FOUND


def test_a_message_that_is_not_json_rpc_is_refused() -> None:
    out = run(server_with_tools().handle([1, 2]))
    assert out is not None and out["error"]["code"] == m.INVALID_REQUEST
    assert run(server_with_tools().handle({"jsonrpc": "2.0", "id": 1, "result": {}})) is None


def test_line_separators_are_escaped_in_the_output() -> None:
    assert " " not in m.dumps({"t": "a b c"})
    assert m.dumps({"t": "é"}) == '{"t":"é"}'


# --- the loop -----------------------------------------------------------------------------------


def serve(server: m.McpServer, lines: list[bytes]) -> list[dict]:
    """Feed ``lines`` to ``server.serve`` and return what it wrote."""
    queue = list(lines)
    written: list[bytes] = []

    async def read_line() -> bytes:
        await asyncio.sleep(0)
        return queue.pop(0) if queue else b""

    run(server.serve(read_line, written.append))
    return [json.loads(w) for w in written]


def line(**msg: Any) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **msg}).encode() + b"\n"


def test_serve_answers_requests_by_id_and_ignores_notifications_and_blank_lines() -> None:
    out = serve(
        server_with_tools(),
        [
            line(id=1, method="initialize", params={"protocolVersion": "2025-11-25"}),
            line(method="notifications/initialized"),
            b"\n",
            line(id="x", method="tools/call", params={"name": "add", "arguments": {"a": 1}}),
        ],
    )
    by_id = {o["id"]: o for o in out}
    assert set(by_id) == {1, "x"}
    assert by_id["x"]["result"]["content"][0]["text"] == '{"sum":2}'


def test_serve_reports_a_parse_error_and_keeps_going() -> None:
    out = serve(server_with_tools(), [b"{not json\n", line(id=2, method="ping")])
    assert out[0]["error"]["code"] == m.PARSE_ERROR and out[0]["id"] is None
    assert out[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_a_slow_tool_does_not_hold_up_a_ping_and_a_cancel_stops_it() -> None:
    server = m.McpServer("t")
    state: dict[str, Any] = {"cancelled": False}

    @server.tool()
    async def slow() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return "late"

    out = serve(
        server,
        [
            line(id=1, method="tools/call", params={"name": "slow", "arguments": {}}),
            line(id=2, method="ping"),
            line(method="notifications/cancelled", params={"requestId": 1}),
        ],
    )
    assert [o["id"] for o in out] == [2], "the ping is answered, the cancelled call is not"
    assert state["cancelled"] is True


def test_the_lifespan_ends_when_input_ends() -> None:
    log: list[str] = []

    @asynccontextmanager
    async def lifespan(_server: m.McpServer) -> AsyncIterator[None]:
        log.append("up")
        try:
            yield
        finally:
            log.append("down")

    serve(m.McpServer("t", lifespan=lifespan), [line(id=1, method="ping")])
    assert log == ["up", "down"]


# --- the real servers ---------------------------------------------------------------------------


def test_every_tool_of_the_cabildo_server_has_a_schema_that_is_json() -> None:
    from cabildo import mcp_server

    tools = run(mcp_server.mcp.list_tools())
    assert len(tools) > 40
    assert len({t.name for t in tools}) == len(tools)
    for tool in tools:
        assert tool.description, tool.name
        json.dumps(tool.listing())


def test_the_browser_server_registers_every_tool() -> None:
    from cabildo.browser import server

    class Dummy(server.Session):  # the tools are only registered, not run
        pass

    tools = run(server.build(Dummy(output_dir=Path("/nonexistent"))).list_tools())  # type: ignore[call-arg]
    assert len(tools) > 15
    for tool in tools:
        json.dumps(tool.listing())


def test_the_real_server_speaks_over_stdio_and_stops_when_stdin_closes(tmp_path: Path) -> None:
    env = {**os.environ, "CABILDO_HOME": str(tmp_path)}
    src = Path(m.__file__).resolve().parents[1]
    env["PYTHONPATH"] = f"{src}{os.pathsep}{env.get('PYTHONPATH', '')}"
    proc = subprocess.Popen(
        [sys.executable, "-c", "from cabildo.mcp_server import main; main()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    for msg in (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ):
        proc.stdin.write(json.dumps(msg).encode() + b"\n")
    proc.stdin.close()
    lines = proc.stdout.read().splitlines()
    assert proc.wait(timeout=30) == 0
    replies = [json.loads(x) for x in lines]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0]["result"]["serverInfo"]["name"] == "cabildo"
    assert len(replies[1]["result"]["tools"]) > 40
