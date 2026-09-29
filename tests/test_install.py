"""Installer client-selection, non-mutating preview, and robustness (#12).

Loads scripts/install.py as a module and drives its steps with the real
subprocess/tool calls stubbed out — CI has neither `uv`, `claude`, nor `codex`,
and we are testing the installer's own dispatch and safety, not the tools.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_INSTALL_PY = _REPO / "scripts" / "install.py"


def _load_install():
    spec = importlib.util.spec_from_file_location("cabildo_install", _INSTALL_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


install = _load_install()

# The stale main checkout's install.py predates --client selection. The push
# gate runs this file against that checkout, so skip there; CI on the branch,
# where install.py has the new API, is authoritative.
if not all(hasattr(install, n) for n in ("install_codex", "preview", "_resolve_clients")):
    pytest.skip("install.py predates --client selection", allow_module_level=True)


@pytest.fixture
def steps(monkeypatch):
    """Replace every mutating step with a recorder, so main() runs pure."""
    calls: list[str] = []
    for name in (
        "ensure_venv",
        "migrate_legacy_home",
        "register_mcp",
        "patch_settings",
        "install_codex",
    ):
        monkeypatch.setattr(install, name, lambda _n=name: calls.append(_n))
    return calls


def test_client_claude_is_default_and_skips_codex(steps):
    assert install.main([]) == 0
    assert "register_mcp" in steps and "patch_settings" in steps
    assert "install_codex" not in steps


def test_client_codex_does_not_touch_claude_settings(steps):
    assert install.main(["--client", "codex"]) == 0
    assert "install_codex" in steps
    # Codex-only must not register the Claude MCP entry or patch settings.json.
    assert "register_mcp" not in steps
    assert "patch_settings" not in steps


def test_client_both_wires_everything(steps):
    assert install.main(["--client", "both"]) == 0
    assert {"register_mcp", "patch_settings", "install_codex"} <= set(steps)


def test_print_is_non_mutating(monkeypatch, capsys):
    # Any mutating step called during --print is a bug: make them all explode.
    def boom(*_a, **_k):
        raise AssertionError("mutating step called during --print")

    for name in (
        "ensure_venv",
        "migrate_legacy_home",
        "register_mcp",
        "patch_settings",
        "install_codex",
        "sh",
    ):
        monkeypatch.setattr(install, name, boom)

    assert install.main(["--print", "--client", "both"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "uv pip install" in out  # describes the install without running it
    assert "codex mcp add cabildo" in out  # real codex command previewed


def test_missing_uv_fails_clearly(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _tool: None)
    with pytest.raises(install.InstallError, match="uv"):
        install.ensure_venv()


def test_main_returns_1_on_install_error(monkeypatch, capsys):
    def fail():
        raise install.InstallError("required executable not found on PATH: 'uv'")

    monkeypatch.setattr(install, "ensure_venv", fail)
    assert install.main([]) == 1
    assert "uv" in capsys.readouterr().err


def test_hook_command_quotes_paths_with_spaces(monkeypatch):
    import shlex

    monkeypatch.setattr(install, "HOOK_BIN", Path("/home/a b/.venv/bin/cabildo-hook"))
    cmd = install.hook_command("session_start")
    # The command must parse back to exactly [path, arg] — no word-splitting on
    # the space in the path.
    assert shlex.split(cmd) == ["/home/a b/.venv/bin/cabildo-hook", "session_start"]


def test_hook_command_ordinary_path_is_unquoted(monkeypatch):
    # For a normal path shlex.quote is a no-op, so existing installs' stored
    # command strings still match and re-running stays idempotent.
    monkeypatch.setattr(install, "HOOK_BIN", Path("/home/j/.venv/bin/cabildo-hook"))
    assert install.hook_command("stop") == "/home/j/.venv/bin/cabildo-hook stop"


def test_patch_settings_preserves_unrelated_and_is_idempotent(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    other = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/other-hook x"}]}
            ]
        },
        "unrelatedKey": {"keep": True},
    }
    settings.write_text(json.dumps(other))
    monkeypatch.setattr(install, "SETTINGS", settings)
    monkeypatch.setattr(install, "HOOK_BIN", Path("/venv/bin/cabildo-hook"))

    install.patch_settings()
    data = json.loads(settings.read_text())
    # Unrelated config and the foreign hook survive.
    assert data["unrelatedKey"] == {"keep": True}
    pre = data["hooks"]["PreToolUse"]
    assert any(h.get("command") == "/opt/other-hook x" for g in pre for h in g.get("hooks", []))
    # Our six events are wired.
    for event in install.HOOKS:
        assert event in data["hooks"]

    # Re-running changes nothing (compare the JSON payload, ignoring the .bak).
    install.patch_settings()
    data2 = json.loads(settings.read_text())
    assert data2 == data
