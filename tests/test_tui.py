"""The TUI's input grammar, tested without a terminal.

parse() decides whether a submitted line quits, retargets, DMs, or posts —
and whether it's urgent. Since urgent now holds an agent's turn open, a
mis-parse here isn't cosmetic: it's the difference between saying something
and interrupting someone.
"""

from __future__ import annotations

import pytest

from cabildo.tui import parse, resolve_topic


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


@pytest.mark.parametrize("line", ["/p paas", "/proj paas", "/p #paas"])
def test_p_jumps_to_project_by_bare_name(line):
    # `/p paas` retargets to the project; resolution to proj:paas happens in
    # submit (needs the topic list), so parse just carries the bare name.
    assert parse(line) == ("switch", {"topic": "paas"})


@pytest.mark.parametrize("line", ["/p", "/proj", "/p   "])
def test_p_alone_lists_projects(line):
    assert parse(line)[0] == "projects"


def test_p_prefixed_speech_still_posts():
    # "/proceed ..." is speech, not the /p command (no space right after /p).
    assert parse("/proceed with the merge")[0] == "send"


def test_resolve_topic_maps_bare_name_to_project_channel():
    existing = {"proj:paas", "general", "proj:agent-chat"}
    assert resolve_topic("paas", existing) == "proj:paas"  # bare -> project
    assert resolve_topic("#paas", existing) == "proj:paas"  # hash stripped
    assert resolve_topic("general", existing) == "general"  # exact match wins
    assert resolve_topic("proj:paas", existing) == "proj:paas"  # already qualified
    assert resolve_topic("brand-new", existing) == "brand-new"  # unknown -> verbatim
