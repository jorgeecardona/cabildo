"""config.py: layered, read-only TOML settings.

Repo-local `.cabildo/config.toml` over `~/.cabildo/config.toml` over built-in
defaults, merged table by table. These tests redirect both paths at tmp dirs, so
nothing reads the developer's real ~/.cabildo, and pass `project_name` explicitly
to keep git out of it.
"""

from __future__ import annotations

import pytest

from cabildo import config


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Point the global config at a tmp file and hand back writers for the global
    and repo-local files plus the repo root to pass as `cwd`.
    """
    global_file = tmp_path / "global.toml"
    monkeypatch.setattr(config, "_global_path", lambda: global_file)
    repo = tmp_path / "repo"
    (repo / ".cabildo").mkdir(parents=True)

    def write_global(text: str) -> None:
        global_file.write_text(text)

    def write_repo(text: str) -> None:
        (repo / ".cabildo" / "config.toml").write_text(text)

    return write_global, str(repo), write_repo


def test_missing_files_read_as_empty(homes):
    _, repo, _ = homes
    assert config.load(repo, project_name="proj") == {}
    assert config.push_mode(repo, "proj") == "land"  # safe default
    assert config.watch_topic(repo, "proj") is None


def test_repo_overrides_global(homes):
    write_global, repo, write_repo = homes
    write_global('[push]\nmode = "land"\n')
    write_repo('[push]\nmode = "manual"\n')
    assert config.push_mode(repo, "proj") == "manual"


def test_tables_merge_across_files(homes):
    # A repo sets [push]; the global file sets [watch]. Both must survive the
    # merge -- one file does not shadow the other's unrelated tables.
    write_global, repo, write_repo = homes
    write_global('[watch]\ntopic = "general"\n')
    write_repo('[push]\nmode = "manual"\n')
    assert config.push_mode(repo, "proj") == "manual"
    assert config.get(repo, "watch", "topic", project_name="proj") == "general"


def test_global_project_section_overrides_top(homes):
    write_global, repo, _ = homes
    write_global('[push]\nmode = "land"\n\n[project.proj]\npush = { mode = "manual" }\n')
    assert config.push_mode(repo, "proj") == "manual"
    assert config.push_mode(repo, "other") == "land"  # a different project keeps the default


def test_project_section_match_is_case_insensitive(homes):
    write_global, repo, _ = homes
    write_global('[project.myrepo]\npush = { mode = "manual" }\n')
    assert config.push_mode(repo, "MyRepo") == "manual"


def test_the_project_table_is_scoping_not_a_setting(homes):
    # `[project.*]` steers other keys; it must never leak into the merged result.
    write_global, repo, _ = homes
    write_global('[project.proj]\npush = { mode = "manual" }\n')
    assert "project" not in config.load(repo, project_name="proj")


def test_malformed_file_is_skipped_not_raised(homes):
    # A typo in the repo file must not take down the guard that reads it: the
    # bad file is dropped and the global file still applies.
    write_global, repo, write_repo = homes
    write_repo("this = = not toml")
    write_global('[push]\nmode = "manual"\n')
    assert config.push_mode(repo, "proj") == "manual"


def test_unknown_push_mode_falls_back_to_land(homes):
    _, repo, write_repo = homes
    write_repo('[push]\nmode = "yolo"\n')
    assert config.push_mode(repo, "proj") == "land"


def test_watch_topic_literal_passes_through(homes):
    _, repo, write_repo = homes
    write_repo('[watch]\ntopic = "proj:foo"\n')
    assert config.watch_topic(repo, "proj") == "proj:foo"


def test_watch_topic_project_sentinel_resolves_to_channel(homes):
    _, repo, write_repo = homes
    write_repo('[watch]\ntopic = "project"\n')
    assert config.watch_topic(repo, "myrepo") == "proj:myrepo"


def test_get_returns_default_when_hop_missing(homes):
    _, repo, write_repo = homes
    write_repo('[watch]\ntopic = "general"\n')
    assert config.get(repo, "push", "mode", project_name="proj", default="land") == "land"
    assert config.get(repo, "watch", "nope", project_name="proj", default=7) == 7


def test_remote_reads_peers_token_bind_and_interval(homes):
    _, repo, write_repo = homes
    write_repo(
        "[remote]\n"
        'peers = ["100.64.0.2:48653", "box.example:48653"]\n'
        'token = "s3cret"\n'
        'bind = "127.0.0.1:48653"\n'
        "interval = 15\n"
    )
    assert config.remote_peers(repo, project_name="proj") == [
        "100.64.0.2:48653",
        "box.example:48653",
    ]
    assert config.remote_token(repo, project_name="proj") == "s3cret"
    assert config.remote_bind(repo, project_name="proj") == "127.0.0.1:48653"
    assert config.remote_interval(repo, project_name="proj") == 15.0


def test_remote_defaults_when_unset(homes):
    _, repo, _ = homes
    assert config.remote_peers(repo, project_name="proj") == []
    assert config.remote_token(repo, project_name="proj") is None
    assert config.remote_bind(repo, project_name="proj") is None
    assert config.remote_interval(repo, project_name="proj") == 30.0


def test_remote_peers_accepts_a_bare_string(homes):
    _, repo, write_repo = homes
    write_repo('[remote]\npeers = "100.64.0.2:48653"\n')
    assert config.remote_peers(repo, project_name="proj") == ["100.64.0.2:48653"]


def test_push_verify_and_timeout_defaults(homes):
    _, repo, _ = homes
    # Default: the daemon push skips the pre-push hook (issue #14) and waits 300s.
    assert config.push_verify(repo, "proj") is False
    assert config.push_timeout(repo, "proj") == 300.0


def test_push_verify_and_timeout_from_config(homes):
    _, repo, write_repo = homes
    write_repo("[push]\nverify = true\ntimeout_s = 45\n")
    assert config.push_verify(repo, "proj") is True
    assert config.push_timeout(repo, "proj") == 45.0
