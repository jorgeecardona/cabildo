"""`cabildo add`: pinning a folder to a project name, and wiring its .mcp.json.

The pin is the interesting half — it overrides the git-directory-name guess that
every session, hook, and MCP server makes when it decides which project it is
in, so getting the precedence right matters more than the file writing.
"""

from __future__ import annotations

import json

import pytest

from cabildo import cli, cloud, context, mcp_config


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    """A throwaway ~ so the registry (and any .mcp.json) stays in tmp."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CABILDO_PROJECT", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_register_root_pins_a_name(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas-infra")
    name, stored = cloud.register_root("paas", str(repo))
    assert (name, stored) == ("paas", str(repo))
    assert cloud.roots_of("paas") == [str(repo)]
    assert cloud.project_for_path(str(repo / "packages" / "api")) == ("paas", str(repo))


def test_a_root_belongs_to_exactly_one_project(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    cloud.register_root("old", str(repo))
    cloud.register_root("new", str(repo))
    assert cloud.roots_of("old") == []
    assert cloud.roots_of("new") == [str(repo)]


def test_deepest_registered_root_wins(cfg_home):
    mono = make_repo(cfg_home / "code" / "mono")
    pkg = mono / "packages" / "api"
    pkg.mkdir(parents=True)
    cloud.register_root("mono", str(mono))
    cloud.register_root("api", str(pkg))
    assert cloud.project_for_path(str(pkg / "src")) == ("api", str(pkg))
    assert cloud.project_for_path(str(mono / "docs")) == ("mono", str(mono))


def test_sibling_prefix_is_not_a_match(cfg_home):
    """/code/mono must not swallow /code/mono-docs."""
    mono = make_repo(cfg_home / "code" / "mono")
    other = make_repo(cfg_home / "code" / "mono-docs")
    cloud.register_root("mono", str(mono))
    assert cloud.project_for_path(str(other)) is None


def test_unregister_keeps_the_projects_settings(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    cloud.register_root("thing", str(repo), uplink="content")
    assert cloud.unregister_root(str(repo)) == "thing"
    assert cloud.roots_of("thing") == []
    assert cloud.uplink_of("thing") == "content"    # a setting, not a consequence
    assert cloud.unregister_root(str(repo)) is None


def test_uplink_and_peers_survive_registering_a_root(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    cloud.set_uplink("thing", "messages")
    cloud.add_peer("thing", "other")
    cloud.register_root("thing", str(repo))
    assert cloud.uplink_of("thing") == "messages"
    assert cloud.peers_of("thing") == ["other"]


def test_register_root_rejects_a_bad_uplink(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    with pytest.raises(ValueError):
        cloud.register_root("thing", str(repo), uplink="everything")


# --------------------------------------------------------------------------- #
# What a session resolves to
# --------------------------------------------------------------------------- #
def test_pin_overrides_the_directory_name(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas-infra")
    assert context.git_project(str(repo))[1] == "paas-infra"
    cloud.register_root("paas", str(repo))
    root, name = context.git_project(str(repo / "deep" if (repo / "deep").exists() else repo))
    assert (root, name) == (str(repo), "paas")


def test_pin_below_the_repo_becomes_its_own_project_root(cfg_home):
    mono = make_repo(cfg_home / "code" / "mono")
    pkg = mono / "packages" / "api"
    pkg.mkdir(parents=True)
    assert context.git_project(str(pkg)) == (str(mono), "mono")
    cloud.register_root("api", str(pkg))
    assert context.git_project(str(pkg)) == (str(pkg), "api")


def test_pin_at_the_repo_only_renames_it(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    sub = repo / "src"
    sub.mkdir()
    cloud.register_root("renamed", str(repo))
    assert context.git_project(str(sub)) == (str(repo), "renamed")


def test_env_var_wins_over_everything(cfg_home, monkeypatch):
    repo = make_repo(cfg_home / "code" / "thing")
    cloud.register_root("pinned", str(repo))
    monkeypatch.setenv("CABILDO_PROJECT", "FromEnv")
    assert context.git_project(str(repo))[1] == "fromenv"


def test_a_broken_registry_does_not_break_sessions(cfg_home):
    """Sessions resolve their project on every prompt; a corrupt config file
    must degrade to the git guess, not take the hook down."""
    repo = make_repo(cfg_home / "code" / "thing")
    p = cloud.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert context.git_project(str(repo)) == (str(repo), "thing")


# --------------------------------------------------------------------------- #
# .mcp.json
# --------------------------------------------------------------------------- #
def test_apply_adds_the_server_and_the_project_env(tmp_path):
    p, what = mcp_config.apply(tmp_path, "paas")
    assert what == "added"
    entry = json.loads(p.read_text())["mcpServers"]["cabildo"]
    assert entry["env"] == {"CABILDO_PROJECT": "paas"}
    assert entry["command"].endswith("cabildo-mcp") or entry["args"][:1] == ["-m"]


def test_apply_is_idempotent_and_leaves_other_servers_alone(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"gitlab": {"command": "gitlab-mcp"}}, "other": 1}))
    mcp_config.apply(tmp_path, "paas")
    p, what = mcp_config.apply(tmp_path, "paas")
    assert what == "unchanged"
    data = json.loads(p.read_text())
    assert data["mcpServers"]["gitlab"] == {"command": "gitlab-mcp"}
    assert data["other"] == 1


def test_apply_reports_an_update_when_the_project_changes(tmp_path):
    mcp_config.apply(tmp_path, "old")
    _, what = mcp_config.apply(tmp_path, "new")
    assert what == "updated"


def test_remove_deletes_only_a_file_that_was_ours(tmp_path):
    mcp_config.apply(tmp_path, "paas")
    p, what = mcp_config.remove(tmp_path)
    assert what == "deleted" and not p.exists()

    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"gitlab": {"command": "gitlab-mcp"}}}))
    mcp_config.apply(tmp_path, "paas")
    p, what = mcp_config.remove(tmp_path)
    assert what == "removed" and p.exists()
    assert list(json.loads(p.read_text())["mcpServers"]) == ["gitlab"]


def test_a_hand_edited_broken_file_is_not_silently_overwritten(tmp_path):
    (tmp_path / ".mcp.json").write_text("{oops")
    with pytest.raises(json.JSONDecodeError):
        mcp_config.apply(tmp_path, "paas")


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def test_add_pins_the_name_and_wires_the_folder(cfg_home, capsys):
    repo = make_repo(cfg_home / "code" / "paas-infra")
    assert cli.main(["add", "--project", "paas", str(repo)]) == 0
    assert cloud.roots_of("paas") == [str(repo)]
    assert json.loads((repo / ".mcp.json").read_text())["mcpServers"]["cabildo"]
    assert context.git_project(str(repo))[1] == "paas"


def test_add_resolves_a_subdir_up_to_the_repo(cfg_home):
    """.mcp.json is only read from the folder Claude Code launches in, so a
    subdirectory has to resolve up or the wiring would do nothing."""
    repo = make_repo(cfg_home / "code" / "thing")
    sub = repo / "src"
    sub.mkdir()
    assert cli.main(["add", str(sub)]) == 0
    assert (repo / ".mcp.json").exists()
    assert not (sub / ".mcp.json").exists()
    assert cloud.roots_of("thing") == [str(repo)]


def test_add_exact_keeps_the_subdir(cfg_home):
    repo = make_repo(cfg_home / "code" / "mono")
    sub = repo / "packages" / "api"
    sub.mkdir(parents=True)
    assert cli.main(["add", "--exact", "-p", "api", str(sub)]) == 0
    assert (sub / ".mcp.json").exists()
    assert cloud.project_for_path(str(sub)) == ("api", str(sub))


def test_add_print_changes_nothing(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    assert cli.main(["add", "--print", "-p", "x", str(repo)]) == 0
    assert not (repo / ".mcp.json").exists()
    assert cloud.project_registry() == {}


def test_add_no_mcp_pins_without_touching_the_repo(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    assert cli.main(["add", "--no-mcp", "-p", "x", str(repo)]) == 0
    assert not (repo / ".mcp.json").exists()
    assert cloud.roots_of("x") == [str(repo)]


def test_add_remove_undoes_both_halves(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    cli.main(["add", "-p", "x", str(repo)])
    assert cli.main(["add", "--remove", str(repo)]) == 0
    assert cloud.roots_of("x") == []
    assert not (repo / ".mcp.json").exists()


def test_add_rejects_a_path_that_is_not_a_directory(cfg_home):
    f = cfg_home / "file.txt"
    f.write_text("hi")
    assert cli.main(["add", str(f)]) == 2


def test_add_takes_uplink_and_link(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    assert cli.main(["add", "-p", "thing", "--uplink", "content",
                     "--link", "other", str(repo)]) == 0
    assert cloud.uplink_of("thing") == "content"
    assert cloud.peers_of("thing") == ["other"]
    assert cloud.peers_of("other") == ["thing"]     # links are symmetric


# --------------------------------------------------------------------------- #
# A pin above a nested repo
#
# From Jorge's real layout: ~/…/self is a git repo (his journal) that must never
# reach the cloud, and ~/…/self/projects/<repo> are separate git repos that may.
# If pinning the parent adopted its nested repos, the parent's uplink would come
# with it — and the inheriting direction of that mistake pushes private
# conversations to the cloud.
# --------------------------------------------------------------------------- #
def test_a_pin_above_a_nested_repo_does_not_adopt_it(cfg_home):
    parent = make_repo(cfg_home / "self")
    child = make_repo(parent / "projects" / "paas")
    cloud.register_root("self", str(parent), uplink="none")

    assert context.git_project(str(parent)) == (str(parent), "self")
    assert context.git_project(str(child)) == (str(child), "paas"), (
        "a nested repo was adopted by its parent's pin"
    )
    assert cloud.uplink_of(context.git_project(str(child))[1]) != "none", (
        "the parent's uplink leaked onto a nested repo"
    )


def test_the_dangerous_direction_too(cfg_home):
    """Parent at `content` must not start pushing a nested repo's conversation."""
    parent = make_repo(cfg_home / "work")
    private = make_repo(parent / "diary")
    cloud.register_root("work", str(parent), uplink="content")
    assert context.git_project(str(private))[1] == "diary"
    assert cloud.uplink_of("diary") == cloud.default_uplink()


def test_a_nested_plain_folder_still_follows_the_pin(cfg_home):
    """Only a nested *repo* escapes. A subdirectory of the pinned folder is part
    of it, which is what makes pinning a repo useful at all."""
    parent = make_repo(cfg_home / "self")
    sub = parent / "inner" / "notes"
    sub.mkdir(parents=True)
    cloud.register_root("self", str(parent), uplink="none")
    assert context.git_project(str(sub)) == (str(parent), "self")


def test_pinning_the_child_explicitly_folds_it_back_in(cfg_home):
    parent = make_repo(cfg_home / "mono")
    child = make_repo(parent / "packages" / "api")
    cloud.register_root("mono", str(parent))
    cloud.register_root("mono", str(child))          # deliberate: same name
    assert context.git_project(str(child))[1] == "mono"


# --------------------------------------------------------------------------- #
# Scratchpad sessions
#
# Claude Code's scratch dir is /tmp/claude-<uid>/<cwd-with-dashes>/<sid>/scratchpad.
# An agent that cd's in there used to become a project called "scratchpad":
# wrong for coordination (it drops out of its team's channel) and wrong for
# privacy — the real project's uplink stops applying, so an opted-out folder
# reappears in the cloud under a name nobody opted out. Found live: an Agent row
# on the dashboard whose real cwd was ~ , pushed as project "scratchpad".
# --------------------------------------------------------------------------- #
def _scratch(base, cwd_path, sid="abc123"):
    mangled = "-" + str(cwd_path).lstrip("/").replace("/", "-")
    p = base / "claude-1000" / mangled / sid / "scratchpad"
    p.mkdir(parents=True)
    return p


def test_a_scratchpad_resolves_to_the_project_it_belongs_to(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas")
    pad = _scratch(cfg_home / "tmp", repo)
    assert context.git_project(str(pad)) == (str(repo), "paas")


def test_the_opted_out_folder_stays_opted_out_from_its_scratchpad(cfg_home):
    """The leak that was live: uplink is applied to the resolved name, so the
    resolution is what enforces it."""
    private = make_repo(cfg_home / "journal")
    cloud.set_uplink("journal", "none")
    pad = _scratch(cfg_home / "tmp", private)
    _root, name = context.git_project(str(pad))
    assert name == "journal"
    assert cloud.uplink_of(name) == "none"


def test_dashes_in_a_directory_name_survive_the_round_trip(cfg_home):
    """The mangling is lossy — '-a-b' is both /a/b and /a-b. The longest path
    that actually exists is the tiebreaker."""
    repo = make_repo(cfg_home / "code" / "agent-chat")
    pad = _scratch(cfg_home / "tmp", repo)
    assert context.git_project(str(pad))[1] == "agent-chat"
    assert context.scratchpad_origin(pad) == repo


def test_an_unrebuildable_scratchpad_falls_back(cfg_home):
    """A deleted origin must not take the hook down or invent a project."""
    pad = _scratch(cfg_home / "tmp", cfg_home / "gone" / "missing")
    root, name = context.git_project(str(pad))
    assert name == "scratchpad" and root == str(pad)


def test_ordinary_paths_are_untouched(cfg_home):
    repo = make_repo(cfg_home / "code" / "thing")
    assert context.scratchpad_origin(repo) is None
    assert context.git_project(str(repo))[1] == "thing"


# --------------------------------------------------------------------------- #
# The dial has to be keyed on what the rows actually carry
#
# Sessions store `project_name` ("paas"); messages and claims store `project`,
# an absolute path. The dials are keyed by name, so every path-keyed lookup
# missed the registry and silently used the default. Live effect: setting
# paas -> messages did nothing (the dashboard's message pane stayed frozen), and
# a `none` project's claims were still being pushed.
# --------------------------------------------------------------------------- #
def test_a_path_resolves_to_the_same_dial_as_its_name(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas")
    cloud.set_uplink("paas", "messages")
    assert cloud.uplink_of("paas") == "messages"
    assert cloud.uplink_of(str(repo)) == "messages", "the path missed the registry"
    assert cloud.uplink_allows(str(repo), "messages")


def test_an_opted_out_project_does_not_leak_through_its_path(cfg_home):
    """The failing-open half: claims carry paths, so `none` wasn't enforced."""
    repo = make_repo(cfg_home / "journal")
    cloud.set_uplink("journal", "none")
    assert cloud.uplink_allows(str(repo), "presence") is False


def test_message_project_returns_a_registry_key_not_a_path(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas")
    assert cloud.message_project("proj:paas", str(repo)) == "paas"
    assert cloud.message_project(None, str(repo)) == "paas"
    assert cloud.message_project("proj:paas", None) == "paas"
    assert cloud.message_project("general", None) is None


def test_a_worktree_path_resolves_to_its_repo(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas")
    wt = repo / ".claude" / "worktrees" / "feature"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/feature\n")
    cloud.set_uplink("paas", "messages")
    assert cloud.uplink_allows(str(wt), "messages"), "a worktree lost its dial"


def test_a_pin_decides_the_dial_for_its_path(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas-infra")
    cloud.register_root("paas", str(repo))
    cloud.set_uplink("paas", "none")
    assert cloud.project_key(str(repo)) == "paas"
    assert cloud.uplink_allows(str(repo), "presence") is False


def test_peers_take_a_path_too(cfg_home):
    repo = make_repo(cfg_home / "code" / "paas")
    cloud.add_peer("paas", "tyko")
    assert cloud.peers_of(str(repo)) == ["tyko"]
    assert cloud.peers_visible(str(repo)) == {"paas", "tyko"}
