"""The agenda: an issues register with the agenda preset. Files are the store, so
every test works on a real folder and none of them touch the bus DB.
"""

from __future__ import annotations

import pytest

from cabildo import agenda, issues


@pytest.fixture
def folder(tmp_path):
    return agenda.ensure(tmp_path / "agenda")


def test_ensure_writes_the_preset(tmp_path):
    d = agenda.ensure(tmp_path / "a")
    assert (d / issues.CONFIG_NAME).exists()
    cfg = agenda.config(d)
    assert cfg.rank_by == "tier"
    assert cfg.closed_needs_fixed_in is False
    assert cfg.singleton_fields == ("highlight",)


def test_add_places_tier_body_and_optional_jira(folder):
    p = agenda.add(
        folder,
        title="Ship the arena",
        tier="NOW",
        body="the description",
        jira="ABC-12",
        today=True,
    )
    issue = issues.load(p, agenda.config(folder))
    assert issue.extra["tier"] == "NOW"
    assert issue.extra["jira"] == "ABC-12"
    assert issue.extra["today"] == "true"
    assert "the description" in issue.body
    assert agenda.check(folder) == []


def test_add_rejects_an_unknown_tier(folder):
    with pytest.raises(issues.IssueFormatError):
        agenda.add(folder, title="x", tier="SOMEDAY")


def test_board_is_now_first(folder):
    agenda.add(folder, title="Later thing", tier="LATER")
    agenda.add(folder, title="Now thing", tier="NOW")
    agenda.add(folder, title="Next thing", tier="NEXT")
    assert [i.title for i in agenda.board(folder).ready] == [
        "Now thing",
        "Next thing",
        "Later thing",
    ]


def test_today_flag_sets_and_clears(folder):
    agenda.add(folder, title="A task", tier="NOW")
    i = agenda.set_flag(folder, "a-task", "today", True)
    assert i.extra.get("today") == "true"
    i = agenda.set_flag(folder, "a-task", "today", False)
    assert "today" not in i.extra  # cleared, not left as an empty key
    assert agenda.check(folder) == []


def test_highlight_is_exclusive_and_moves(folder):
    agenda.add(folder, title="First", tier="NOW")
    agenda.add(folder, title="Second", tier="NEXT")
    agenda.set_highlight(folder, "first")
    agenda.set_highlight(folder, "second")  # moving it must clear the first
    lit = [i for i in issues.load_all(folder, agenda.config(folder))[0] if i.extra.get("highlight")]
    assert [i.title for i in lit] == ["Second"]
    assert agenda.check(folder) == []  # the singleton rule is satisfied


def test_mark_done_needs_no_sha_and_stays_clean(folder):
    agenda.add(folder, title="Finish it", tier="NOW")
    done = agenda.mark_done(folder, "finish-it")
    assert done.status is issues.Status.DONE
    assert done.fixed_in is None
    assert agenda.check(folder) == []  # closed_needs_fixed_in is false for an agenda


def test_global_dir_honors_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CABILDO_AGENDA_DIR", str(tmp_path / "home-agenda"))
    d = agenda.resolve_dir("global", create=True)
    assert d == tmp_path / "home-agenda"
    assert (d / issues.CONFIG_NAME).exists()


def test_project_dir_walks_up_to_docs_agenda(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs" / "agenda").mkdir(parents=True)
    (repo / "src").mkdir()
    assert agenda.project_dir(repo / "src") == repo / "docs" / "agenda"


def test_project_dir_stops_at_the_git_root(tmp_path):
    # An agenda above the repo root belongs to something else and must not be adopted.
    (tmp_path / "docs" / "agenda").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert agenda.project_dir(repo) is None


# --------------------------------------------------------------------------- #
# The agenda MCP tools — thin verbs over the store, isolated from the bus
# --------------------------------------------------------------------------- #
@pytest.fixture
def agenda_tools(tmp_path, monkeypatch):
    from cabildo import issues_mcp

    monkeypatch.setenv("CABILDO_AGENDA_DIR", str(tmp_path / "home-agenda"))
    monkeypatch.setattr(issues_mcp, "_identity", lambda: (None, None))  # no bus in these tests
    return issues_mcp


def test_agenda_tools_add_board_done_roundtrip(agenda_tools):
    assert agenda_tools.agenda_add("Ship it", "NOW", scope="global").startswith("added")
    board = agenda_tools.agenda_board(scope="global")
    assert "Ship it" in board and "NOW" in board
    assert agenda_tools.agenda_done("ship-it", scope="global").startswith("done")
    assert "1 done" in agenda_tools.agenda_board(scope="global")


def test_agenda_tools_reject_a_bad_tier(agenda_tools):
    assert "not added" in agenda_tools.agenda_add("x", "SOMEDAY", scope="global")


def test_agenda_tools_today_and_exclusive_highlight(agenda_tools):
    agenda_tools.agenda_add("First", "NOW", scope="global")
    agenda_tools.agenda_add("Second", "NEXT", scope="global")
    assert agenda_tools.agenda_today("first", scope="global").startswith("today")
    agenda_tools.agenda_highlight("first", scope="global")
    agenda_tools.agenda_highlight("second", scope="global")  # moves the single highlight
    board = agenda_tools.agenda_board(scope="global")
    assert board.count("★") == 1
