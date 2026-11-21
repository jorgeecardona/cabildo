"""The names file keeps what the machine cannot compute: handles, roles, titles, tags."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cabildo import cli, names, names_file, store
from cabildo.workspace import sessions as ws


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def named(conn, sid: str, handle: str | None = None, role: str | None = None) -> None:
    store.register_session(conn, sid, cwd="/p", project="/p", project_name="p")
    if handle:
        store.set_handle(conn, sid, handle)
    if role:
        store.set_role(conn, sid, role, "reviews")


def test_capture_keeps_only_sessions_you_named_and_the_sidecar(home, conn) -> None:
    named(conn, "aaaa1111", handle="boss")
    named(conn, "bbbb2222", role="reviewer")
    named(conn, "cccc3333")  # no handle, no role: its robot name is computed
    ws.update_meta("aaaa1111", lambda m: ws.Meta(label="the plan", tags=("x",)))
    snap = names_file.capture(conn)
    assert [s.session_id for s in snap.sessions] == ["aaaa1111", "bbbb2222"]
    assert snap.sessions[1].responsibilities == "reviews"
    assert dict(snap.meta)["aaaa1111"].label == "the plan"


def test_the_file_round_trips_and_shows_the_robot_name(home, conn) -> None:
    named(conn, "aaaa1111", handle="boss")
    snap = names_file.capture(conn)
    text = names_file.render(snap)
    assert json.loads(text)["sessions"][0]["name"] == names.robot_name("aaaa1111")
    assert names_file.parse(text) == snap


def test_write_skips_a_file_that_has_not_changed(home, conn, tmp_path: Path) -> None:
    named(conn, "aaaa1111", handle="boss")
    target = tmp_path / "n.json"
    snap = names_file.capture(conn)
    assert names_file.write(snap, target) is True
    first = target.read_text()
    assert names_file.write(snap, target) is False
    assert target.read_text() == first
    named(conn, "bbbb2222", handle="second")
    assert names_file.write(names_file.capture(conn), target) is True


def test_a_foreign_file_is_replaced_not_trusted(home, conn, tmp_path: Path) -> None:
    target = tmp_path / "n.json"
    target.write_text("not json")
    assert names_file.write(names_file.capture(conn), target) is True
    assert names_file.parse(target.read_text()) == names_file.Snapshot()


@pytest.mark.parametrize(
    "text",
    [
        "nope",
        "[]",
        '{"version": 9}',
        '{"version": 1, "sessions": [{"handle": "x"}]}',
        '{"version": 1, "meta": []}',
    ],
)
def test_parse_refuses_a_file_that_is_not_ours(text: str) -> None:
    with pytest.raises(names_file.NamesFileError):
        names_file.parse(text)


def test_import_adds_unknown_sessions_as_ended_and_fills_only_empty_names(home, conn) -> None:
    named(conn, "aaaa1111", handle="mine")  # already named: must stay
    named(conn, "bbbb2222")  # known, no handle: gets the saved one
    snap = names_file.Snapshot(
        sessions=(
            names_file.BusName("aaaa1111", handle="old", role="lead"),
            names_file.BusName("bbbb2222", handle="saved"),
            names_file.BusName("dddd4444", handle="gone", cwd="/q"),
        )
    )
    done = names_file.restore(conn, snap)
    rows = {r["session_id"]: r for r in conn.execute("select * from sessions")}
    assert rows["aaaa1111"]["handle"] == "mine"
    assert rows["aaaa1111"]["role"] == "lead"
    assert rows["bbbb2222"]["handle"] == "saved"
    assert (rows["dddd4444"]["handle"], rows["dddd4444"]["status"]) == ("gone", "ended")
    assert done == names_file.Restored(added=1, filled=2, kept=1)


def test_import_fills_empty_sidecar_fields_and_keeps_your_title(home, conn) -> None:
    ws.update_meta("aaaa1111", lambda m: ws.Meta(label="mine"))
    snap = names_file.Snapshot(
        meta=(
            ("aaaa1111", ws.Meta(label="old", group="work", tags=("t",))),
            ("bbbb2222", ws.Meta(label="new one")),
        )
    )
    done = names_file.restore(conn, snap)
    meta = ws.load_meta()
    assert (meta["aaaa1111"].label, meta["aaaa1111"].group, meta["aaaa1111"].tags) == (
        "mine",
        "work",
        ("t",),
    )
    assert meta["bbbb2222"].label == "new one"
    assert done.kept == 1


def test_the_file_goes_where_the_config_says(home: Path) -> None:
    assert names_file.path() == home / "names.json"
    (home / "config.toml").write_text('[names]\nfile = "~/Sync/n.json"\n')
    assert names_file.path() == Path("~/Sync/n.json").expanduser()


def test_sync_logs_an_unwritable_path_and_goes_on(home, conn, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    (home / "config.toml").write_text(f'[names]\nfile = "{blocker}/n.json"\n')
    logged: list[str] = []
    names_file.sync(conn, logged.append)
    assert "cannot write" in logged[0]


def test_export_then_import_on_a_new_machine(home, conn, tmp_path: Path, capsys) -> None:
    named(conn, "aaaa1111", handle="boss", role="lead")
    target = tmp_path / "backup.json"
    assert cli.main(["names", "export", "--file", str(target)]) == 0
    assert "1 handle/role" in capsys.readouterr().out
    conn.execute("delete from sessions")
    conn.commit()
    assert cli.main(["names", "import", "--file", str(target)]) == 0
    row = conn.execute("select handle, role, status from sessions").fetchone()
    assert tuple(row) == ("boss", "lead", "ended")


def test_import_of_a_missing_file_says_so(home, tmp_path: Path, capsys) -> None:
    assert cli.main(["names", "import", "--file", str(tmp_path / "none.json")]) == 2
    assert "cannot read" in capsys.readouterr().err
