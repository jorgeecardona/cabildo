"""The list of ``cabildo`` CLI commands is fixed: adding or removing one must change this test."""

from __future__ import annotations

import contextlib
import io
import re

import pytest

from cabildo import cli

COMMANDS = [
    "watch",
    "say",
    "log",
    "whoami",
    "pull",
    "wait",
    "send",
    "mark-read",
    "meter",
    "install",
    "plugin",
    "sessions",
    "clip",
    "jupyter",
    "gateway",
    "stats",
    "host",
    "attach",
    "names",
    "stop",
    "mem",
    "load",
    "code",
    "browser",
    "cost",
    "pair",
    "wan",
    "project",
    "add",
    "codex",
    "opencode",
    "minutes",
    "messages",
    "ci",
    "friction",
    "improve",
    "notes",
    "service",
    "viewer",
    "doctor",
    "cron",
    "mcp",
    "githook",
    "guard",
]


def test_help_lists_exactly_these_commands() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        cli.main(["--help"])
    found = re.search(r"\{([^}]*)\}", out.getvalue())
    assert found is not None
    assert found.group(1).split(",") == COMMANDS


def test_mcp_subcommand_runs_the_mcp_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cabildo mcp`` starts the same server as ``cabildo-mcp``, for ``uvx cabildo mcp``."""
    from cabildo import mcp_server

    calls: list[str] = []
    monkeypatch.setattr(mcp_server, "main", lambda: calls.append("ran"))
    assert cli.main(["mcp"]) == 0
    assert calls == ["ran"]
