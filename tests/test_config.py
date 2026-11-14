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
    assert config.push_mode(repo, "proj") == "auto"  # safe default
    assert config.watch_topic(repo, "proj") is None


def test_repo_project_name_pins_identity(homes):
    _, repo, write_repo = homes
    assert config.repo_project_name(repo) is None  # no file -> no pin
    write_repo('[project]\nname = "Cabildo-OSS"\n')
    assert config.repo_project_name(repo) == "cabildo-oss"  # lowered


def test_repo_project_name_ignores_a_scoping_table(homes):
    # `[project.foo]` is a scoping table, not a `name` pin -> no identity claimed.
    _, repo, write_repo = homes
    write_repo("[project.foo]\nisolated = true\n")
    assert config.repo_project_name(repo) is None


def test_project_isolated_reads_the_scoped_flag(homes):
    write_global, repo, _ = homes
    assert config.project_isolated(repo, "self") is False  # default
    write_global("[project.self]\nisolated = true\n")
    assert config.project_isolated(repo, "self") is True
    assert config.project_isolated(repo, "crm") is False  # scoped to `self` only


def test_repo_overrides_global(homes):
    write_global, repo, write_repo = homes
    write_global('[push]\nmode = "auto"\n')
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
    write_global('[push]\nmode = "auto"\n\n[project.proj]\npush = { mode = "manual" }\n')
    assert config.push_mode(repo, "proj") == "manual"
    assert config.push_mode(repo, "other") == "auto"  # a different project keeps the default


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


def test_unknown_push_mode_falls_back_to_auto(homes):
    _, repo, write_repo = homes
    write_repo('[push]\nmode = "yolo"\n')
    assert config.push_mode(repo, "proj") == "auto"


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
    assert config.get(repo, "push", "mode", project_name="proj", default="auto") == "auto"
    assert config.get(repo, "watch", "nope", project_name="proj", default=7) == 7


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


def test_roles_empty_by_default(homes):
    _, repo, _ = homes
    assert config.roles(repo, project_name="proj") == {}


def test_roles_maps_name_to_responsibilities_lowercased(homes):
    _, repo, write_repo = homes
    write_repo(
        '[roles]\nReviewer = "Review diffs before push"\narchitect = "Own module boundaries"\n'
    )
    got = config.roles(repo, project_name="proj")
    assert got == {
        "reviewer": "Review diffs before push",
        "architect": "Own module boundaries",
    }


def test_roles_ignores_non_string_entries(homes):
    # A [roles] entry that is not a string (a typo'd nested table) is dropped
    # rather than raising into set_role's resolution.
    _, repo, write_repo = homes
    write_repo('[roles]\ngood = "ok"\n[roles.bad]\nx = 1\n')
    assert config.roles(repo, project_name="proj") == {"good": "ok"}


def test_roles_repo_overrides_global(homes):
    write_global, repo, write_repo = homes
    write_global('[roles]\nreviewer = "global text"\n')
    write_repo('[roles]\nreviewer = "repo text"\n')
    assert config.roles(repo, project_name="proj")["reviewer"] == "repo text"


def test_spend_mode_defaults_to_auto(homes):
    _, repo, _ = homes
    assert config.spend_mode(repo, "proj") == "auto"


def test_spend_mode_reads_the_workspace_table(homes):
    _, repo, write_repo = homes
    write_repo('[workspace]\nspend = "always"\n')
    assert config.spend_mode(repo, "proj") == "always"
    write_repo('[workspace]\nspend = "never"\n')
    assert config.spend_mode(repo, "proj") == "never"


def test_an_unknown_spend_mode_falls_back_to_auto(homes):
    _, repo, write_repo = homes
    write_repo('[workspace]\nspend = "sometimes"\n')
    assert config.spend_mode(repo, "proj") == "auto"
