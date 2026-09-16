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


# --------------------------------------------------------------------------- #
# Importing a flat roadmap.md into per-task files
# --------------------------------------------------------------------------- #
SAMPLE_ROADMAP = """\
# Roadmap

## NOW
- [ ] First task: do the thing (ABC-1). @today
  a note line
  another note line
- [~] Second task, in progress

## NEXT
- [x] Already done in next (ABC-2)
- [ ] Plain next @highlight
- [ ] Second highlight tries to sneak in @highlight

## DONE
- [x] An old finished thing
"""


def test_parse_roadmap_maps_marks_tiers_flags_and_body():
    tasks, warnings = agenda.parse_roadmap(SAMPLE_ROADMAP)
    first = tasks[0]
    assert first.tier == "NOW" and first.status is issues.Status.OPEN and first.today
    assert first.jira == "ABC-1"
    assert first.body == "a note line\nanother note line"
    assert tasks[1].status is issues.Status.IN_PROGRESS
    done_next = next(t for t in tasks if t.title.startswith("Already done"))
    assert done_next.tier == "NEXT" and done_next.status is issues.Status.DONE
    old = next(t for t in tasks if t.title == "An old finished thing")
    assert old.tier is None and old.status is issues.Status.DONE  # DONE heading = status, not tier
    # exactly one highlight survives, the second is dropped with a warning
    assert sum(t.highlight for t in tasks) == 1
    assert any("second @highlight" in w.lower() or "highlight" in w.lower() for w in warnings)


def test_import_tasks_writes_clean_files(tmp_path):
    folder = tmp_path / "agenda"
    tasks, _ = agenda.parse_roadmap(SAMPLE_ROADMAP)
    counts = agenda.import_tasks(folder, tasks, owner="me")
    assert counts == {"open": 3, "in-progress": 1, "done": 2}
    assert agenda.check(folder) == []  # migrated register is green
    found, broken = issues.load_all(folder, agenda.config(folder))
    assert broken == ()
    assert sum(bool(i.extra.get("highlight")) for i in found) == 1
    # in-progress carries an owner (the register requires one); done needs no sha
    ip = next(i for i in found if i.status is issues.Status.IN_PROGRESS)
    assert ip.owner == "me"


def test_import_tasks_avoids_filename_collisions(tmp_path):
    folder = tmp_path / "agenda"
    dup = "## NOW\n- [ ] Same title here now\n- [ ] Same title here now\n"
    agenda.import_tasks(folder, agenda.parse_roadmap(dup)[0])
    files = [p.name for p in folder.glob("*.md")]
    assert len(files) == 2 and len(set(files)) == 2  # two distinct filenames


# --------------------------------------------------------------------------- #
# The view: claim = flow. A task's sessions are who holds its issue:<slug> lease.
# --------------------------------------------------------------------------- #
def _claim(conn, sid, slug):
    from cabildo import store

    store.acquire_claim(
        conn,
        resource=f"issue:{slug}",
        holder_session=sid,
        holder_handle=None,
        project=None,
        ttl_seconds=600,
        note=None,
    )


def test_view_matches_sessions_gaps_fragmentation_and_to_track(conn, tmp_path):
    from tests.conftest import make_session

    folder = agenda.ensure(tmp_path / "ag")
    agenda.add(folder, title="Do today", tier="NOW", today=True, jira="ABC-1")
    agenda.add(folder, title="Also today", tier="NEXT", today=True)  # no jira, no session
    agenda.add(folder, title="Someday", tier="LATER")
    make_session(conn, "sa", handle="alice")
    _claim(conn, "sa", "do-today")
    _claim(conn, "sa", "someday")  # one session across two tasks -> fragmentation

    v = agenda.view(conn, folder)
    assert {t["title"] for t in v["today"]} == {"Do today", "Also today"}
    do = next(t for t in v["today"] if t["slug"] == "do-today")
    assert [s["session"] for s in do["sessions"]] == ["@alice"]  # the exclusive holder
    assert do["sessions"][0]["running"]  # freshly seen => warm
    assert do["jira"] == "ABC-1"
    sig = v["signals"]
    assert sig["live_wip"] == 2  # two tasks have a warm holder
    frag = next(f for f in sig["fragmentation"] if f["session"] == "@alice")
    assert set(frag["slugs"]) == {"do-today", "someday"}  # @alice spread across both
    assert "also-today" in sig["gaps"]  # a today task nobody is on
    assert "also-today" in sig["to_track"]  # a today task with no jira key
    assert "someday" in sig["to_track"]


def test_view_drift_is_a_warm_session_off_a_today_task(conn, tmp_path):
    from tests.conftest import make_session

    folder = agenda.ensure(tmp_path / "ag")
    agenda.add(folder, title="Do today", tier="NOW", today=True)
    agenda.add(folder, title="Side quest", tier="NEXT")
    make_session(conn, "on", handle="onit")
    make_session(conn, "off", handle="drifter")
    _claim(conn, "on", "do-today")  # on a today task -> not drift
    _claim(conn, "off", "side-quest")  # warm, but off today -> drift

    drift = agenda.view(conn, folder)["signals"]["drift"]
    who = {d["session"]: d["on"] for d in drift}
    assert who.get("@drifter") == ["side-quest"]  # held tasks, none of them a today task
    assert "@onit" not in who  # on a today task -> not drift


def test_view_without_a_bus_still_returns_the_structure(tmp_path):
    folder = agenda.ensure(tmp_path / "ag")
    agenda.add(folder, title="A task", tier="NOW", today=True)
    v = agenda.view(None, folder)
    assert v["today"][0]["sessions"] == []
    assert v["signals"]["drift"] == []
    assert v["counts"]["open"] == 1


def test_view_attaches_cost_from_usage_to_the_claimed_task(conn, tmp_path, monkeypatch):
    from cabildo import usage
    from tests.conftest import make_session

    folder = agenda.ensure(tmp_path / "ag")
    agenda.add(folder, title="Costly task", tier="NOW", today=True)
    make_session(conn, "sx", handle="dev")
    _claim(conn, "sx", "costly-task")
    monkeypatch.setattr(
        usage,
        "records",
        lambda since: iter([{"session": "sx", "weighted": 12.5, "in": 100, "out": 20}]),
    )
    v = agenda.view(conn, folder)
    do = next(t for t in v["today"] if t["slug"] == "costly-task")
    assert do["cost_weighted"] == 12.5  # the session's cost lands on its task
    assert do["sessions"][0]["tokens_in"] == 100
    assert v["counts"]["cost_weighted_today"] == 12.5
