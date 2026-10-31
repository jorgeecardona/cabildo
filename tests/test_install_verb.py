"""``cabildo install``: the packaged installer wires hooks, the MCP server and the mod."""

from __future__ import annotations

import json
from pathlib import Path

from cabildo import install


def test_patch_settings_wires_every_hook_and_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"keep": 1}))
    hook_bin, cabildo_bin = Path("/v/bin/cabildo-hook"), Path("/v/bin/cabildo")

    install.patch_settings(settings, hook_bin, cabildo_bin)
    first = json.loads(settings.read_text())
    assert first["keep"] == 1
    assert set(first["hooks"]) == set(install.HOOKS)
    assert first["hooks"]["StopFailure"][0]["matcher"] == "rate_limit"
    assert first["env"]["CABILDO_BIN"] == str(cabildo_bin)

    install.patch_settings(settings, hook_bin, cabildo_bin)
    assert json.loads(settings.read_text()) == first


def test_run_wires_mcp_settings_and_plugin_in_order(monkeypatch, tmp_path):
    steps: list[str] = []
    monkeypatch.setattr(install, "register_mcp", lambda _p: steps.append("mcp"))
    monkeypatch.setattr(install, "patch_settings", lambda *_a: steps.append("settings"))
    monkeypatch.setattr(install, "register_plugin", lambda: steps.append("plugin"))
    assert install.run(("claude",)) == 0
    assert steps == ["mcp", "settings", "plugin"]


def test_print_changes_nothing(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("must not run")

    for name in ("register_mcp", "patch_settings", "register_plugin"):
        monkeypatch.setattr(install, name, boom)
    assert install.run(("claude",), print_only=True) == 0
    assert "StopFailure" in capsys.readouterr().out
