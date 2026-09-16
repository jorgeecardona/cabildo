"""The TUI's input grammar, tested without a terminal.

parse() decides whether a submitted line quits, retargets, DMs, or posts —
and whether it's urgent. Since urgent now holds an agent's turn open, a
mis-parse here isn't cosmetic: it's the difference between saying something
and interrupting someone.
"""

from __future__ import annotations

import pytest

from cabildo.tui import InputHistory, agenda_rows, completions, parse, resolve_topic


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


# --------------------------------------------------------------------------- #
# InputHistory — ↑/↓ recall of sent lines
# --------------------------------------------------------------------------- #
def test_history_up_walks_backwards_then_down_returns():
    h = InputHistory()
    for line in ("first", "second", "third"):
        h.record(line)
    # up from a fresh, empty draft recalls newest -> oldest
    assert h.up("") == "third"
    assert h.up("third") == "second"
    assert h.up("second") == "first"
    assert h.up("first") == "first"  # clamped at the oldest
    # down walks back toward newest
    assert h.down() == "second"
    assert h.down() == "third"


def test_history_down_restores_the_stashed_draft():
    h = InputHistory()
    h.record("sent")
    assert h.up("half-typed") == "sent"  # draft stashed on first step back
    assert h.down() == "half-typed"  # stepping past newest restores it
    assert h.down() is None  # no longer browsing -> leave the buffer alone


def test_history_empty_and_not_browsing_are_noops():
    h = InputHistory()
    assert h.up("draft") is None  # nothing recorded
    assert h.down() is None  # not browsing
    h.record("x")
    assert h.down() is None  # down without a prior up does nothing


def test_history_skips_blanks_and_immediate_repeats():
    h = InputHistory()
    h.record("")
    h.record("   ")
    h.record("keep")
    h.record("keep")  # immediate duplicate not stored
    h.record("other")
    assert h.items == ["keep", "other"]


def test_recording_resets_browsing_position():
    h = InputHistory()
    h.record("a")
    h.record("b")
    assert h.up("") == "b"  # browsing
    h.record("c")  # submitting a new line returns to live composing
    assert h.up("") == "c"  # next up starts from the newest again


# --------------------------------------------------------------------------- #
# The agenda view in `cabildo watch`
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("line", ["/agenda", "/a"])
def test_agenda_command_is_recognised(line):
    assert parse(line) == ("agenda", {})


def test_agenda_toggle_does_not_swallow_ordinary_speech():
    # "/agendas of the meeting" is a message, not the command.
    assert parse("/agendas of the meeting")[0] == "send"


def test_agenda_rows_render_tiers_titles_and_markers(tmp_path):
    from cabildo import agenda

    folder = agenda.ensure(tmp_path / "ag")
    agenda.add(folder, title="Do the thing", tier="NOW", today=True)
    agenda.add(folder, title="Later item", tier="LATER")
    agenda.set_highlight(folder, "do-the-thing")
    rows = agenda_rows(agenda.board(folder))
    text = "\n".join("".join(seg[0] for seg in row) for row in rows)
    assert "READY" in text
    assert "NOW" in text and "Do the thing" in text
    assert "★" in text  # the exclusive highlight marker
    assert "•" in text  # the @today marker


# --------------------------------------------------------------------------- #
# @agent / #channel completion
# --------------------------------------------------------------------------- #
AGENTS = ["jorge", "alice", "rusty-robocop-0a750"]
CHANNELS = ["general", "proj:cabildo", "proj:maths"]


def test_at_completes_agents_by_prefix():
    assert completions("@al", AGENTS, CHANNELS) == ["@alice"]
    assert completions("@", AGENTS, CHANNELS) == ["@jorge", "@alice", "@rusty-robocop-0a750"]


def test_hash_completes_channels_by_prefix():
    assert completions("#proj", AGENTS, CHANNELS) == ["#proj:cabildo", "#proj:maths"]
    assert completions("#general", AGENTS, CHANNELS) == ["#general"]


def test_completion_is_case_insensitive():
    assert completions("@AL", AGENTS, CHANNELS) == ["@alice"]


def test_a_plain_word_completes_nothing():
    assert completions("hello", AGENTS, CHANNELS) == []
    assert completions("", AGENTS, CHANNELS) == []
