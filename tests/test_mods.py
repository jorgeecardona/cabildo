"""The bundled-mod installer (`cabildo.mods`) and its `cabildo plugin` CLI verb.

The module single-sources the `claude plugin` sequence that both
`scripts/install.py` and the CLI run. A fake runner records the argv it is
handed, so the tests pin the exact commands and their order without a real
`claude` on PATH.
"""

from __future__ import annotations

from cabildo import cli, mods


class FakeRun:
    """A runner that records each argv and returns a scripted exit code."""

    def __init__(self, codes=None):
        self.calls: list[list[str]] = []
        self._codes = dict(codes or {})

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        # key on the verb (argv[2]) so a test can fail one step
        verb = argv[2] if len(argv) > 2 else ""
        return self._codes.get(verb, 0)


# --------------------------------------------------------------------------- #
# mods.install / remove / plan
# --------------------------------------------------------------------------- #
def test_install_runs_validate_then_add_install_enable_update(monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    run = FakeRun()
    res = mods.install(run, scope="user")
    assert res.ok is True
    verbs = [c[2] for c in run.calls]
    assert verbs == ["validate", "marketplace", "install", "enable", "update"]
    # install/enable carry the plugin id and the scope.
    assert run.calls[2] == ["claude", "plugin", "install", mods.PLUGIN_ID, "--scope", "user"]
    assert run.calls[3] == ["claude", "plugin", "enable", mods.PLUGIN_ID, "--scope", "user"]


def test_install_stops_when_validate_fails(monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    run = FakeRun(codes={"validate": 1})
    res = mods.install(run)
    assert res.ok is False
    assert res.reason is not None and "validate" in res.reason
    # Nothing past validate ran.
    assert [c[2] for c in run.calls] == ["validate"]


def test_install_without_claude_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: None)
    run = FakeRun()
    res = mods.install(run)
    assert res.ok is False
    assert "PATH" in (res.reason or "")
    assert run.calls == []


def test_install_without_mods_dir_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(mods, "mods_dir", lambda: None)
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    run = FakeRun()
    res = mods.install(run)
    assert res.ok is False
    assert "bundled mod" in (res.reason or "")
    assert run.calls == []


def test_install_can_skip_the_update_step(monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    run = FakeRun()
    mods.install(run, update=False)
    assert "update" not in [c[2] for c in run.calls]


def test_remove_uninstalls_at_scope(monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    run = FakeRun()
    res = mods.remove(run, scope="user")
    assert res.ok is True
    assert run.calls == [["claude", "plugin", "uninstall", mods.PLUGIN_ID, "--scope", "user"]]


def test_mods_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CABILDO_MODS_DIR", str(tmp_path))
    assert mods.mods_dir() == tmp_path
    monkeypatch.setenv("CABILDO_MODS_DIR", str(tmp_path / "does-not-exist"))
    assert mods.mods_dir() is None


def test_plan_mirrors_the_install_sequence():
    verbs = [cmd[2] for cmd in mods.plan()]
    assert verbs == ["validate", "marketplace", "install", "enable", "update"]


# --------------------------------------------------------------------------- #
# `cabildo plugin` CLI verb
# --------------------------------------------------------------------------- #
def test_cli_plugin_print_changes_nothing(capsys, monkeypatch):
    # Even with no `claude`, --print just lists the plan.
    monkeypatch.setattr(mods.shutil, "which", lambda _c: None)
    assert cli.main(["plugin", "--print"]) == 0
    out = capsys.readouterr().out
    assert "claude plugin install cabildo-push@cabildo-mods --scope user" in out


def test_cli_plugin_install_success(capsys, monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    monkeypatch.setattr(mods.subprocess, "call", lambda _argv: 0)
    assert cli.main(["plugin"]) == 0
    assert "installed + enabled" in capsys.readouterr().out


def test_cli_plugin_skip_without_claude_exits_nonzero(capsys, monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: None)
    assert cli.main(["plugin"]) == 1
    assert "skipped" in capsys.readouterr().out


def test_cli_plugin_remove(capsys, monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    seen: list[list[str]] = []
    monkeypatch.setattr(mods.subprocess, "call", lambda argv: seen.append(argv) or 0)
    assert cli.main(["plugin", "--remove"]) == 0
    assert seen == [["claude", "plugin", "uninstall", mods.PLUGIN_ID, "--scope", "user"]]
    assert "removed" in capsys.readouterr().out


def test_cli_plugin_install_at_project_scope(capsys, monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    seen: list[list[str]] = []
    monkeypatch.setattr(mods.subprocess, "call", lambda argv: seen.append(argv) or 0)
    assert cli.main(["plugin", "--scope", "project"]) == 0
    assert ["claude", "plugin", "install", mods.PLUGIN_ID, "--scope", "project"] in seen
