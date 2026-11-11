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
    "stop",
    "mem",
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
    "friction",
    "improve",
    "notes",
    "issues",
    "service",
    "viewer",
    "doctor",
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
