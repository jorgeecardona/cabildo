"""The TUI's input grammar, tested without a terminal.

parse() decides whether a submitted line quits, retargets, DMs, or posts —
and whether it's urgent. Since urgent now holds an agent's turn open, a
mis-parse here isn't cosmetic: it's the difference between saying something
and interrupting someone.
"""

from __future__ import annotations

import pytest

from backchat.tui import parse


@pytest.mark.parametrize("line", ["/q", "/quit", "/exit"])
def test_quit(line):
    assert parse(line)[0] == "quit"


@pytest.mark.parametrize("line", ["", "   ", "/u", "/u   ", "@name", "/t", "/t  "])
def test_incomplete_lines_do_nothing(line):
    assert parse(line)[0] == "noop"


def test_plain_line_posts_to_current_topic():
    kind, p = parse("hello agents")
    assert kind == "send"
    assert p == {"body": "hello agents", "urgent": False, "to": None}


def test_at_name_is_a_dm():
    kind, p = parse("@uptime how is the gate?")
    assert kind == "send"
    assert p == {"body": "how is the gate?", "urgent": False, "to": "uptime"}


def test_urgent_channel_message():
    kind, p = parse("/u stop pushing")
    assert kind == "send"
    assert p == {"body": "stop pushing", "urgent": True, "to": None}


def test_urgent_dm_composes():
    kind, p = parse("/u @uptime answer me")
    assert kind == "send"
    assert p == {"body": "answer me", "urgent": True, "to": "uptime"}


@pytest.mark.parametrize("line", ["/t proj:paas", "/topic proj:paas", "/t #proj:paas"])
def test_switch_topic_strips_hash(line):
    assert parse(line) == ("switch", {"topic": "proj:paas"})


def test_slash_words_that_are_not_commands_still_post():
    # A line that merely starts with a slash is speech, not a command.
    kind, p = parse("/usr/bin/python is the wrong interpreter")
    assert kind == "send"
    assert p["body"].startswith("/usr/bin/python")
