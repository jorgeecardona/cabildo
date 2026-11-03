"""rooms.py: the project and topics of a session, read from the bus DB."""

from __future__ import annotations

from pathlib import Path

from cabildo import store
from cabildo.workspace import rooms
from cabildo.workspace.rooms import NO_ROOMS, Rooms


def register(session_id: str, project_name: str, *topics: str) -> None:
    conn = store.connect()
    store.register_session(conn, session_id, project_name=project_name)
    for topic in topics:
        store.ensure_topic(conn, topic)
        store.subscribe(conn, session_id, topic)
    conn.commit()
    conn.close()


def test_load_returns_project_and_sorted_topics_of_a_registered_session() -> None:
    register("s1", "cabildo", "proj:cabildo", "general", "design")
    found = rooms.load(["s1"])
    assert found["s1"] == Rooms(
        project="cabildo",
        topics=("design", "general", "proj:cabildo"),
        scopes=(("proj:cabildo", "project"),),
    )


def test_load_leaves_out_a_session_the_bus_does_not_know() -> None:
    register("s1", "cabildo")
    assert set(rooms.load(["s1", "unknown"])) == {"s1"}


def test_load_is_empty_when_there_is_no_bus_db(bus_home: Path) -> None:
    assert not bus_home.exists()
    assert rooms.load(["s1"]) == {}
    assert not bus_home.exists()  # reading never creates the DB


def test_load_with_no_ids_does_not_open_the_db(bus_home: Path) -> None:
    assert rooms.load([]) == {}
    assert not bus_home.exists()


def test_label_shows_each_topic_with_a_hash_and_nothing_for_no_rooms() -> None:
    assert rooms.label(Rooms("cabildo", ("general", "proj:cabildo"))) == "#general #proj:cabildo"
    assert rooms.label(NO_ROOMS) == ""


def test_the_panel_row_shows_the_rooms_on_its_second_line(transcript) -> None:
    from lines import user

    from cabildo.workspace import sessions
    from cabildo.workspace.app import SwitcherApp

    transcript("s1", user("Fix the login bug"))
    register("s1", "cabildo", "general", "proj:cabildo")
    [row] = sessions.build_rows(None)
    assert row.rooms == NO_ROOMS  # build_rows reads transcripts only; the app joins the bus

    [joined] = sessions.attach_rooms([row])
    text = SwitcherApp._session_text(joined, 100)
    first, second = text.plain.split("\n")
    assert "Fix the login bug" in first
    assert second.endswith("#general #proj:cabildo(project)")


def test_the_panel_row_without_a_bus_entry_has_no_rooms_text(transcript) -> None:
    from lines import user

    from cabildo.workspace import sessions
    from cabildo.workspace.app import SwitcherApp

    transcript("s1", user("Fix the login bug"))
    [row] = sessions.build_rows(None)
    assert "#" not in SwitcherApp._session_text(row, 80).plain


def test_the_second_line_never_exceeds_the_width_and_keeps_the_rooms(transcript) -> None:
    from dataclasses import replace

    from lines import user

    from cabildo.workspace import sessions
    from cabildo.workspace.app import SwitcherApp

    transcript("s1", user("Fix the login bug", branch="main"))
    [row] = sessions.build_rows(None)
    topics = ("alpha", "beta", "general", "gamma", "proj:cabildo")
    for width in (14, 30, 40, 80):
        for n in (0, 2, 5):
            r = replace(row, rooms=Rooms("cabildo", topics[:n]))
            second = SwitcherApp._session_text(r, width).plain.split("\n")[1]
            assert len(second) <= width, (width, n, second)
            if n and width >= 30:
                assert "#" in second, (width, n, second)


def test_short_path_with_no_room_returns_at_most_an_ellipsis() -> None:
    from cabildo.workspace.app import short_path

    assert short_path("/a/long/path", 1) == "…"
    assert short_path("/a/long/path", 0) == ""
    assert short_path("/a/long/path", -3) == ""


def test_load_handles_more_ids_than_sqlite_allows_in_one_query() -> None:
    register("s1", "cabildo", "general")
    ids = [f"x{i}" for i in range(1500)] + ["s1"]
    assert set(rooms.load(ids)) == {"s1"}


def test_label_shows_the_scope_of_a_topic_that_leaves_the_machine() -> None:
    r = Rooms("cabildo", ("general", "proj:cabildo"), (("proj:cabildo", "project"),))
    assert rooms.label(r) == "#general #proj:cabildo(project)"
