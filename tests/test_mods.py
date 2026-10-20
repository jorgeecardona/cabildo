"""The bundled-mod installer (`cabildo.mods`) and its `cabildo plugin` CLI verb.

The module single-sources the `claude plugin` sequence that both
`scripts/install.py` and the CLI run. A fake runner records the argv it is
handed, so the tests pin the exact commands and their order without a real
`claude` on PATH.
"""

from __future__ import annotations

import pytest

from cabildo import cli, mods


@pytest.fixture(autouse=True)
def _clean_mods_env(monkeypatch):
    """Pin `mods_dir()` to the real repo `mods/` by clearing any inherited
    `CABILDO_MODS_DIR` — otherwise a value in the ambient environment points the
    installer elsewhere and these tests' "mod found" assumption breaks.
    """
    monkeypatch.delenv("CABILDO_MODS_DIR", raising=False)


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


def test_mods_dir_finds_the_packaged_copy(monkeypatch, tmp_path):
    # A plain wheel install has no repo `mods/`; the mod ships beside the package
    # as `_bundled_mods/`. Point _PKG_DIR at a fake site-packages layout.
    pkg = tmp_path / "site-packages" / "cabildo"
    bundled = pkg / "_bundled_mods"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(mods, "_PKG_DIR", pkg)
    assert mods.mods_dir() == bundled


def test_mods_dir_env_override_beats_the_packaged_copy(monkeypatch, tmp_path):
    pkg = tmp_path / "cabildo"
    (pkg / "_bundled_mods").mkdir(parents=True)
    monkeypatch.setattr(mods, "_PKG_DIR", pkg)
    override = tmp_path / "custom"
    override.mkdir()
    monkeypatch.setenv("CABILDO_MODS_DIR", str(override))
    assert mods.mods_dir() == override


def test_step_warnings_lists_only_non_zero_steps_and_stays_honest():
    res = mods.Result(
        ok=True,
        steps=(
            mods.Step(argv=("claude", "plugin", "install", "x"), rc=0),
            mods.Step(argv=("claude", "plugin", "enable", "x"), rc=1),
        ),
    )
    lines = mods.step_warnings(res)
    assert len(lines) == 1
    # The line names the failing command and does NOT assert the cause is benign.
    assert "enable" in lines[0]
    assert "real failure" in lines[0]
    assert "re-run" in lines[0]
    # All-zero → no warnings.
    assert mods.step_warnings(mods.Result(ok=True, steps=())) == []


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


def test_cli_plugin_install_at_project_scope(monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    seen: list[list[str]] = []
    monkeypatch.setattr(mods.subprocess, "call", lambda argv: seen.append(argv) or 0)
    assert cli.main(["plugin", "--scope", "project"]) == 0
    assert ["claude", "plugin", "install", mods.PLUGIN_ID, "--scope", "project"] in seen


def test_cli_plugin_remove_print_shows_uninstall_not_install(capsys, monkeypatch):
    # A removal preview must not list the install plan (Fable review #3).
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    assert cli.main(["plugin", "--remove", "--print"]) == 0
    out = capsys.readouterr().out
    assert "uninstall" in out
    assert "marketplace add" not in out


def test_cli_plugin_flags_a_failed_step(capsys, monkeypatch):
    # install() tolerates a non-zero enable, but the failure must still surface
    # rather than hide behind the green line (Fable review #1).
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    monkeypatch.setattr(mods.subprocess, "call", lambda argv: 7 if argv[2] == "enable" else 0)
    assert cli.main(["plugin"]) == 0
    assert "exit 7" in capsys.readouterr().out


def test_remove_reports_a_failed_uninstall(monkeypatch):
    monkeypatch.setattr(mods.shutil, "which", lambda _c: "/usr/bin/claude")
    res = mods.remove(FakeRun(codes={"uninstall": 3}))
    assert res.ok is False
    assert "3" in (res.reason or "")
