"""A folder that is not a project gets no shared proj: channel, and name clashes are reported (#26)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cabildo import config, context, doctor, store
from cabildo.store import project_topic


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp/home with a repo `app`, a folder `projects` that holds repos, and a plain folder `notes`."""
    home = tmp_path / "home"
    (home / "projects" / "app" / ".git").mkdir(parents=True)
    (home / "projects" / "lib" / ".git").mkdir(parents=True)
    (home / "notes").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path / "bus"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(config, "_global_path", lambda: tmp_path / "global.toml")
    return home


def test_the_home_folder_and_a_folder_of_repos_are_catch_alls(tree: Path) -> None:
    assert context.is_catch_all(str(tree))
    assert context.is_catch_all(str(tree / "projects"))


def test_a_repo_a_plain_folder_and_a_missing_path_are_not(tree: Path) -> None:
    assert not context.is_catch_all(str(tree / "projects" / "app"))
    assert not context.is_catch_all(str(tree / "notes"))
    assert not context.is_catch_all(str(tree / "nope"))
    assert not context.is_catch_all(None)


def _subscribed(conn, sid: str, name: str) -> bool:
    return any(t == project_topic(name) for t in store.subscriptions_of(conn, sid))


def test_a_session_in_a_catch_all_folder_joins_no_project_channel(conn, tree: Path) -> None:
    store.register_session(
        conn,
        "c",
        cwd=str(tree / "projects"),
        project=str(tree / "projects"),
        project_name="projects",
    )
    store.register_session(
        conn,
        "r",
        cwd=str(tree / "projects" / "app"),
        project=str(tree / "projects" / "app"),
        project_name="app",
    )
    assert not _subscribed(conn, "c", "projects")
    assert _subscribed(conn, "r", "app")


def test_isolated_false_in_config_keeps_the_channel_of_a_catch_all(
    conn, tree: Path, tmp_path: Path
) -> None:
    (tmp_path / "global.toml").write_text("[project.projects]\nisolated = false\n")
    store.register_session(
        conn,
        "c",
        cwd=str(tree / "projects"),
        project=str(tree / "projects"),
        project_name="projects",
    )
    assert _subscribed(conn, "c", "projects")


def test_doctor_names_two_folders_that_share_a_project_name(conn, tree: Path) -> None:
    for sid, root in (("a", "/work/oss/cabildo"), ("b", "/work/self/cabildo")):
        store.register_session(conn, sid, cwd=root, project=root, project_name="cabildo")
    store.register_session(
        conn, "c", cwd="/work/other", project="/work/other", project_name="other"
    )
    check = doctor.check_project_names(conn)
    assert check.status == doctor.WARN
    assert "/work/oss/cabildo" in check.detail and "/work/self/cabildo" in check.detail
    assert ".cabildo/config.toml" in check.fix


def test_doctor_is_quiet_when_every_name_has_one_folder(conn, tree: Path) -> None:
    store.register_session(conn, "a", cwd="/work/a", project="/work/a", project_name="a")
    store.register_session(conn, "b", cwd="/work/a", project="/work/a", project_name="a")
    assert doctor.check_project_names(conn).status == doctor.OK
