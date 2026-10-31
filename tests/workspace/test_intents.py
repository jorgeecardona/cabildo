"""intents.py and the panel's intents group: the push queue and its latest results."""

from __future__ import annotations

from pathlib import Path

from cabildo import store
from cabildo.workspace import app, intents
from cabildo.workspace.intents import Intent

NOW = 1_000_000.0


def queue(
    project: str, *, status: str = "pending", at: float = NOW, reason: str | None = None
) -> int:
    with store.connect() as conn:
        iid = store.request_intent(
            conn,
            from_session="s1",
            from_handle="jorge",
            project=f"/home/x/{project}",
            action="push",
            params={"cwd": f"/home/x/{project}", "target": "main"},
        )
        conn.execute("UPDATE intents SET created_at=? WHERE id=?", (at, iid))
        if status != "pending":
            result = '{"reason": "%s"}' % reason if reason else None
            conn.execute(
                "UPDATE intents SET status=?, finished_at=?, result=? WHERE id=?",
                (status, at, result, iid),
            )
        conn.commit()
    return iid


def test_load_lists_the_queue_running_first_then_the_latest_finished() -> None:
    queue("timbre", at=NOW - 60)
    running = queue("cabildo", status="running", at=NOW - 120)
    queue("paas", status="done", at=NOW - 30)
    found = intents.load(NOW)
    assert [(i.project, i.status) for i in found] == [
        ("cabildo", "running"),
        ("timbre", "pending"),
        ("paas", "done"),
    ]
    assert found[0].id == running and found[0].by == "jorge" and found[0].waiting


def test_load_keeps_a_failure_reason_to_its_first_line() -> None:
    queue("cabildo", status="failed", at=NOW - 10, reason="dirty tree\\nM a.py")
    [failed] = intents.load(NOW)
    assert failed.reason == "dirty tree" and not failed.waiting


def test_load_drops_finished_intents_older_than_an_hour_and_caps_them() -> None:
    queue("old", status="done", at=NOW - intents.RECENT_SECONDS - 1)
    for i in range(intents.RECENT_MAX + 2):
        queue(f"p{i}", status="done", at=NOW - 10 + i)
    found = intents.load(NOW)
    assert len(found) == intents.RECENT_MAX and all(i.project != "old" for i in found)
    assert found[0].project == f"p{intents.RECENT_MAX + 1}"  # newest first


def test_load_is_empty_without_a_db_and_never_creates_it(bus_home: Path) -> None:
    assert intents.load(NOW) == ()
    assert not bus_home.exists()


def test_fmt_age_uses_one_unit() -> None:
    assert [app.fmt_age(s) for s in (5, 59, 60, 3599, 3600, 90000)] == [
        "5s",
        "59s",
        "1m",
        "59m",
        "1h",
        "1d",
    ]


def row(
    status: str, reason: str = "", at: float = NOW - 120, cwd: str = "/home/x/cabildo"
) -> Intent:
    name = cwd.rsplit("/", 1)[-1]
    return Intent(1, "push", status, name, cwd, name, "jorge", at, reason)


def test_the_group_shows_the_queue_and_a_failure_with_its_reason() -> None:
    lines = app.fmt_intents((row("running"), row("failed", "dirty tree")), NOW)
    text = [x for x, _ in lines]
    assert text[0].startswith("intents") and "state" in text[0]
    assert text[1].startswith("push cabildo") and "running" in text[1] and "2m" in text[1]
    assert "failed" in text[2] and text[3] == "  dirty tree"
    assert lines[2][1] == "bold red" and lines[3][1] == "red" and lines[1][1] == ""


def test_the_group_says_none_for_an_empty_queue() -> None:
    assert app.fmt_intents((), NOW) == [("intents    none in the last hour", "")]


def test_the_group_counts_what_does_not_fit() -> None:
    many = tuple(row("pending") for _ in range(app.INTENT_LINES + 3))
    lines = app.fmt_intents(many, NOW)
    assert len(lines) <= app.INTENT_LINES + 2
    assert lines[-1][0].startswith("+") and lines[-1][0].endswith("more")


def test_the_group_leaves_out_a_failure_that_has_no_room_for_its_reason() -> None:
    items = (*(row("pending") for _ in range(app.INTENT_LINES - 1)), row("failed", "dirty tree"))
    lines = [x for x, _ in app.fmt_intents(items, NOW)]
    assert not any("failed" in x or "dirty tree" in x for x in lines)
    assert lines[-1] == "+1 more"


def test_the_mode_line_shows_the_intents_group_and_grows_to_fit_it(monkeypatch) -> None:
    from pilot import drive

    seen: list = []

    async def scenario(app, pilot) -> None:
        app.scope = "all"
        app._intents_got((row("running"), row("failed", "dirty tree")))
        await pilot.pause()
        box = app.query_one("#modeline")
        lines = str(box.render()).split("\n")
        seen.append((box.size.height, len(lines), lines))

    drive(monkeypatch, scenario)
    height, n, lines = seen[0]
    assert height == n
    assert any(x.startswith("push cabildo") and "running" in x for x in lines)
    assert any("dirty tree" in x for x in lines)


def test_scope_here_keeps_only_the_project_of_the_workspace() -> None:
    mine, other = row("running"), row("running", cwd="/home/x/timbre")
    assert intents.in_scope((mine, other), "here", "/home/x/cabildo", "cabildo") == (mine,)


def test_scope_subfolders_adds_the_repos_under_the_directory() -> None:
    mine = row("running")
    below = row("pending", cwd="/home/x/oss/paas")
    beside = row("pending", cwd="/home/x/ossy")  # a name that only starts the same way
    got = intents.in_scope((mine, below, beside), "subfolders", "/home/x/oss", "oss")
    assert got == (below,)


def test_scope_all_keeps_every_intent() -> None:
    items = (row("running"), row("done", cwd="/home/x/timbre"))
    assert intents.in_scope(items, "all", "/home/x/cabildo", "cabildo") == items


def test_the_mode_line_follows_the_scope(monkeypatch) -> None:
    from pilot import drive

    seen: list[list[str]] = []

    async def scenario(app, pilot) -> None:
        app._cwd_key = "cabildo"
        app.cwd = "/home/x/cabildo"
        app._intents_got((row("running"), row("failed", "dirty", cwd="/home/x/timbre")))
        await pilot.pause()
        seen.append(str(app.query_one("#modeline").render()).split("\n"))
        app.scope = "all"
        app._modeline()
        await pilot.pause()
        seen.append(str(app.query_one("#modeline").render()).split("\n"))

    drive(monkeypatch, scenario)
    here, everything = seen
    assert any(x.startswith("push cabildo") for x in here)
    assert not any(x.startswith("push timbre") for x in here)
    assert any(x.startswith("push timbre") for x in everything)
