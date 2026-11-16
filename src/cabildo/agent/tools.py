"""The built-in tools: read, edit, bash, grep."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from .types import Context, Tool

_MAX_OUT = 20_000


def _path(args: Mapping[str, object], ctx: Context, key: str = "path") -> Path:
    p = (ctx.cwd / str(args[key])).resolve()
    if not p.is_relative_to(ctx.cwd.resolve()):
        msg = f"path {p} is outside {ctx.cwd}"
        raise ValueError(msg)
    return p


def _read(args: Mapping[str, object], ctx: Context) -> str:
    return _path(args, ctx).read_text()[:_MAX_OUT]


def _edit(args: Mapping[str, object], ctx: Context) -> str:
    p = _path(args, ctx)
    old, new = str(args["old"]), str(args["new"])
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        return f"error: 'old' must match exactly once, it matched {count} times"
    p.write_text(text.replace(old, new))
    return "ok"


def _run(cmd: list[str], ctx: Context) -> str:
    done = subprocess.run(
        cmd, cwd=ctx.cwd, capture_output=True, text=True, timeout=120, check=False
    )
    return (done.stdout + done.stderr)[:_MAX_OUT] or f"(exit {done.returncode}, no output)"


def _bash(args: Mapping[str, object], ctx: Context) -> str:
    return _run(["bash", "-c", str(args["command"])], ctx)


def _grep(args: Mapping[str, object], ctx: Context) -> str:
    return _run(["grep", "-rn", "--", str(args["pattern"]), str(args.get("path", "."))], ctx)


def _schema(**props: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
        "required": list(props),
    }


TOOLS: tuple[Tool, ...] = (
    Tool("read", "Read a file.", _schema(path="File path."), _read),
    Tool(
        "edit",
        "Replace one exact string in a file. 'old' must appear exactly once.",
        _schema(path="File path.", old="Exact text to replace.", new="Replacement text."),
        _edit,
        writes=True,
    ),
    Tool("bash", "Run a shell command.", _schema(command="The command."), _bash),
    Tool("grep", "Search files for a pattern.", _schema(pattern="Pattern."), _grep),
)
