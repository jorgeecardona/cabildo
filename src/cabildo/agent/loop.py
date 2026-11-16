"""The agent loop: ask the model, validate each tool call, run it, repeat."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .client import chat
from .types import Context, Endpoint, Message, Tool, ToolCall

MAX_STEPS = 30
MAX_BAD_CALLS = 2  # consecutive invalid calls before the loop gives up


def check_call(call: ToolCall, tools: Mapping[str, Tool]) -> str | None:
    """Return an error message when ``call`` is not valid for its tool, else None."""
    tool = tools.get(call.name)
    if tool is None:
        return f"error: unknown tool '{call.name}'. Tools: {', '.join(sorted(tools))}"
    if "__invalid__" in call.args:
        return "error: arguments were not valid JSON"
    required = tool.parameters.get("required", [])
    assert isinstance(required, list)  # noqa: S101
    missing = [k for k in required if k not in call.args]
    return f"error: missing arguments: {', '.join(missing)}" if missing else None


def run_call(call: ToolCall, tools: Mapping[str, Tool], ctx: Context) -> str:
    """Run one tool call. Every failure becomes text the model can read."""
    if (err := check_call(call, tools)) is not None:
        return err
    tool = tools[call.name]
    try:
        if tool.writes:
            holder = ctx.claims.acquire((ctx.cwd / str(call.args[tool.write_path_arg])).resolve())
            if holder is not None:
                return f"error: {call.args[tool.write_path_arg]} is claimed by {holder}"
        return tool.run(call.args, ctx)
    except (OSError, ValueError) as exc:
        return f"error: {exc}"


def run(
    task: str,
    *,
    endpoint: Endpoint,
    tools: Sequence[Tool],
    ctx: Context,
    system: str = "You are a coding agent. Use the tools to finish the task, then answer briefly.",
    ask: Callable[[Endpoint, Sequence[Message], Sequence[Tool]], Message] = chat,
) -> tuple[Message, ...]:
    """Run ``task`` until the model stops calling tools. Returns the full transcript."""
    by_name = {t.name: t for t in tools}
    messages: tuple[Message, ...] = (Message("system", system), Message("user", task))
    bad = 0
    for _ in range(MAX_STEPS):
        reply = ask(endpoint, messages, tools)
        messages = (*messages, reply)
        if not reply.calls:
            return messages
        results = tuple(
            Message("tool", run_call(c, by_name, ctx), call_id=c.id) for c in reply.calls
        )
        messages = (*messages, *results)
        bad = bad + 1 if all(r.text.startswith("error:") for r in results) else 0
        if bad > MAX_BAD_CALLS:
            return (*messages, Message("assistant", "stopped: too many invalid tool calls"))
    return (*messages, Message("assistant", "stopped: step limit reached"))
