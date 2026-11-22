"""A stdio MCP server with no SDK: JSON-RPC 2.0, one message per line, on stdin and stdout.

Claude Code starts each cabildo MCP server as a child process and talks to it over stdio. This
module is all the protocol those servers need, so cabildo does not depend on the ``mcp`` package
(which brings starlette, uvicorn and a dozen more for transports cabildo never uses).

It speaks the ``initialize`` era of MCP (``2024-11-05`` to ``2025-11-25``). It declares only the
``tools`` capability and answers ``server/discover``, the request of the newer era, with
"method not found". A client that speaks both eras then falls back to ``initialize``.

A tool is a plain function. Its parameter types become the JSON Schema of its input, and its
docstring becomes its description::

    server = McpServer("demo")

    @server.tool()
    def add(a: int, b: int = 1) -> dict[str, int]:
        \"\"\"Add two numbers.\"\"\"
        return {"sum": a + b}

    server.run()

The decorator returns the function unchanged, so a test can call it directly.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import os
import re
import sys
import types
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, BinaryIO, Literal, Union, get_args, get_origin, get_type_hints

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
_SCALARS: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}
_INTEGER = re.compile(r"-?\d+")
_NUMBER = re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
_UNION = (Union, types.UnionType)


class ToolError(Exception):
    """A tool could not run. The client shows the message to the model."""


class RpcError(Exception):
    """A JSON-RPC protocol error: an unknown method or tool, or malformed parameters."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Image:
    """An image a tool returns. ``format`` is ``png`` or ``jpeg``."""

    data: bytes
    format: str = "png"


@dataclass(frozen=True)
class TextBlock:
    """A text part of a tool result."""

    text: str
    type: str = "text"


@dataclass(frozen=True)
class ImageBlock:
    """An image part of a tool result. ``data`` is base64."""

    data: str
    mimeType: str  # noqa: N815
    type: str = "image"


Block = TextBlock | ImageBlock


@dataclass(frozen=True)
class Param:
    """One parameter of a tool."""

    name: str
    hint: Any
    required: bool
    default: Any = None


@dataclass(frozen=True)
class Tool:
    """A callable the server offers: its name, description, parameters and function."""

    name: str
    description: str
    params: tuple[Param, ...]
    fn: Callable[..., Any]

    @property
    def input_schema(self) -> dict[str, Any]:
        """The JSON Schema of the arguments."""
        props: dict[str, Any] = {}
        for p in self.params:
            schema = schema_of(p.hint)
            props[p.name] = schema if p.required else {**schema, "default": p.default}
        out: dict[str, Any] = {"type": "object", "properties": props}
        required = [p.name for p in self.params if p.required]
        if required:
            out["required"] = required
        return out

    def listing(self) -> dict[str, Any]:
        """The entry for ``tools/list``."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def schema_of(hint: Any) -> dict[str, Any]:
    """The JSON Schema of a Python type hint.

    Raises:
        TypeError: for a type this module does not map, so a bad tool fails when it is defined.
    """
    if hint is Any or hint is object:
        return {}
    if hint is None or hint is type(None):
        return {"type": "null"}
    origin = get_origin(hint)
    if origin is Literal:
        values = list(get_args(hint))
        kinds = {_SCALARS.get(type(v)) for v in values}
        return (
            {"enum": values, "type": kinds.pop()}
            if len(kinds) == 1 and None not in kinds
            else {"enum": values}
        )
    if origin in _UNION:
        return {"anyOf": [schema_of(a) for a in get_args(hint)]}
    if hint in _SCALARS:
        return {"type": _SCALARS[hint]}
    if hint is list or origin is list:
        args = get_args(hint)
        return {"type": "array", "items": schema_of(args[0])} if args else {"type": "array"}
    if hint is dict or origin is dict:
        args = get_args(hint)
        return (
            {"type": "object", "additionalProperties": schema_of(args[1]) or True}
            if len(args) == 2
            else {"type": "object"}
        )
    msg = f"unsupported parameter type {hint!r}"
    raise TypeError(msg)


def _kind(value: Any) -> str:
    return type(value).__name__


def coerce(hint: Any, value: Any) -> Any:
    """``value`` as ``hint``, the way a model's loose JSON needs it.

    Models send ``"5"`` for an integer or an array as a JSON string, so those are accepted.

    Raises:
        ValueError: with a short reason when the value does not fit.
    """
    if hint is Any or hint is object:
        return value
    if hint is None or hint is type(None):
        if value is None:
            return None
        msg = f"expected null, got {_kind(value)}"
        raise ValueError(msg)
    origin = get_origin(hint)
    if origin is Literal:
        if any(value == a and type(value) is type(a) for a in get_args(hint)):
            return value
        msg = f"expected one of {list(get_args(hint))}, got {value!r}"
        raise ValueError(msg)
    if origin in _UNION:
        options = get_args(hint)
        if value is None and type(None) in options:
            return None
        for option in options:
            if option is type(None):
                continue
            with contextlib.suppress(ValueError):
                return coerce(option, value)
        msg = f"does not match any of {[getattr(o, '__name__', str(o)) for o in options]}"
        raise ValueError(msg)
    if hint is str:
        if isinstance(value, str):
            return value
        msg = f"expected a string, got {_kind(value)}"
        raise ValueError(msg)
    if hint is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        msg = f"expected a boolean, got {_kind(value)}"
        raise ValueError(msg)
    if hint is int:
        if isinstance(value, bool):
            msg = "expected an integer, got bool"
            raise ValueError(msg)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
            return int(value)
        msg = f"expected an integer, got {_kind(value)}"
        raise ValueError(msg)
    if hint is float:
        if isinstance(value, bool):
            msg = "expected a number, got bool"
            raise ValueError(msg)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
            return float(value)
        msg = f"expected a number, got {_kind(value)}"
        raise ValueError(msg)
    if hint is list or origin is list:
        items = _json_if_text(value)
        if not isinstance(items, list):
            msg = f"expected an array, got {_kind(value)}"
            raise ValueError(msg)
        args = get_args(hint)
        return [coerce(args[0], v) for v in items] if args else items
    if hint is dict or origin is dict:
        mapping = _json_if_text(value)
        if not isinstance(mapping, dict):
            msg = f"expected an object, got {_kind(value)}"
            raise ValueError(msg)
        args = get_args(hint)
        return {k: coerce(args[1], v) for k, v in mapping.items()} if len(args) == 2 else mapping
    msg = f"unsupported type {hint!r}"
    raise ValueError(msg)


def _json_if_text(value: Any) -> Any:
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return json.loads(value)
    return value


def make_tool(name: str, fn: Callable[..., Any]) -> Tool:
    """A ``Tool`` for ``fn``: parameters from its signature and type hints, text from its doc.

    Raises:
        TypeError: when a parameter has no type hint or an unsupported one.
    """
    hints = get_type_hints(fn)
    params: list[Param] = []
    for p in inspect.signature(fn).parameters.values():
        if p.name not in hints:
            msg = f"tool {name}: parameter {p.name} has no type hint"
            raise TypeError(msg)
        schema_of(hints[p.name])  # fail now, not at the first tools/list
        required = p.default is inspect.Parameter.empty
        params.append(Param(p.name, hints[p.name], required, None if required else p.default))
    return Tool(name, inspect.getdoc(fn) or "", tuple(params), fn)


def to_blocks(result: Any) -> list[Block]:
    """The content blocks for a tool's return value."""
    if result is None:
        return []
    if isinstance(result, str):
        return [TextBlock(result)]
    if isinstance(result, Image):
        return [ImageBlock(base64.b64encode(result.data).decode("ascii"), f"image/{result.format}")]
    return [TextBlock(dumps(result))]


def dumps(value: Any) -> str:
    """Compact JSON on one line. The line separators U+2028 and U+2029 are escaped, because a
    client's line reader may split on them.
    """
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


class McpServer:
    """The tools of one server, and the loop that serves them over stdio."""

    def __init__(
        self,
        name: str,
        *,
        instructions: str | None = None,
        lifespan: Callable[[McpServer], AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self._lifespan = lifespan
        self._tools: dict[str, Tool] = {}
        self._running: dict[Any, asyncio.Task[None]] = {}

    def tool(
        self, *, name: str | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a function as a tool. ``name`` overrides the function's name."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool = make_tool(name or fn.__name__, fn)
            if tool.name in self._tools:
                msg = f"tool {tool.name} is registered twice"
                raise ValueError(msg)
            self._tools[tool.name] = tool
            return fn

        return register

    async def list_tools(self) -> list[Tool]:
        """The registered tools, in the order they were defined."""
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> list[Block]:
        """Run a tool and return its content blocks.

        Raises:
            RpcError: when the tool does not exist.
            ToolError: when the arguments do not fit or the tool raises.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        given = arguments or {}
        kwargs: dict[str, Any] = {}
        problems: list[str] = []
        for p in tool.params:
            if p.name not in given:
                if p.required:
                    problems.append(f"{p.name}: required")
                continue
            try:
                kwargs[p.name] = coerce(p.hint, given[p.name])
            except ValueError as exc:
                problems.append(f"{p.name}: {exc}")
        if problems:
            msg = f"Invalid arguments for {name}: " + "; ".join(problems)
            raise ToolError(msg)
        try:
            result = tool.fn(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        except ToolError:
            raise
        except Exception as exc:
            msg = f"Error executing tool {name}: {exc}"
            raise ToolError(msg) from exc
        return to_blocks(result)

    async def handle(self, message: Any) -> dict[str, Any] | None:
        """The response to one JSON-RPC message, or None for a notification or a stray response."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        if "method" not in message:
            return None  # a response: this server sends no requests, so it expects none
        is_request = "id" in message
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return (
                _error(message.get("id"), INVALID_PARAMS, "params must be an object")
                if is_request
                else None
            )
        try:
            result = await self._dispatch(str(message["method"]), params)
        except RpcError as exc:
            return _error(message["id"], exc.code, exc.message) if is_request else None
        except Exception as exc:  # a bug in a handler must not end the server
            print(f"{self.name}: internal error: {exc!r}", file=sys.stderr, flush=True)
            return _error(message["id"], INTERNAL_ERROR, "internal error") if is_request else None
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.listing() for t in self._tools.values()]}
        if method == "tools/call":
            return await self._call(params)
        if method.startswith("notifications/"):
            return {}
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        from . import __version__

        asked = params.get("protocolVersion")
        info: dict[str, Any] = {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": __version__},
        }
        if self.instructions:
            info["instructions"] = self.instructions
        return info

    async def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments or {}, dict):
            raise RpcError(INVALID_PARAMS, "tools/call needs a name and an arguments object")
        try:
            blocks = await self.call_tool(name, arguments)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [b.__dict__ for b in blocks], "isError": False}

    async def serve(self, read_line: Callable[[], Any], write: Callable[[bytes], None]) -> None:
        """Serve messages until ``read_line`` returns b"" (end of input).

        ``read_line`` is awaitable. Requests run as tasks, so a slow tool does not hold up a
        ``ping`` or a cancellation. On the way out the running tasks are cancelled and the
        lifespan ends.
        """
        lifespan = self._lifespan(self) if self._lifespan else contextlib.nullcontext()
        async with lifespan:
            try:
                while line := await read_line():
                    self._accept(line, write)
            finally:
                tasks = list(self._running.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _accept(self, line: bytes, write: Callable[[bytes], None]) -> None:
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except ValueError:
            write(_encode(_error(None, PARSE_ERROR, "parse error")))
            return
        if isinstance(message, dict) and message.get("method") == "notifications/cancelled":
            task = self._running.get((message.get("params") or {}).get("requestId"))
            if task is not None:
                task.cancel()
            return
        key = message.get("id") if isinstance(message, dict) and "method" in message else None

        async def run() -> None:
            try:
                response = await self.handle(message)
            finally:
                self._running.pop(key, None)
            if response is not None:
                write(_encode(response))

        if key is None:
            asyncio.get_running_loop().create_task(run())
            return
        self._running[key] = asyncio.get_running_loop().create_task(run())

    def run(self) -> None:
        """Serve this process's stdin and stdout until stdin closes.

        Anything a tool prints to stdout would corrupt the stream, so the real stdout is moved
        to a private descriptor and file descriptor 1 is pointed at stderr.
        """
        stdin: BinaryIO = sys.stdin.buffer
        out = os.fdopen(os.dup(1), "wb")
        os.dup2(2, 1)

        def write(data: bytes) -> None:
            out.write(data + b"\n")
            out.flush()

        async def read_line() -> bytes:
            return await asyncio.get_running_loop().run_in_executor(None, stdin.readline)

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(self.serve(read_line, write))


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _encode(message: dict[str, Any]) -> bytes:
    return dumps(message).encode("utf-8")
