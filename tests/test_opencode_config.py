"""Wiring opencode onto the bus: the cabildo MCP entry, tagged opencode, merged into
opencode's JSON config.
"""

from __future__ import annotations

import json

import pytest

from cabildo import opencode_config as oc


@pytest.fixture
def opencode_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "opencode"


def test_apply_writes_the_cabildo_entry_tagged_opencode(opencode_home):
    p, what = oc.apply(project="cabildo", home="/opt/cabildo-home")
    assert what == "added"
    data = json.loads(p.read_text())
    entry = data["mcp"]["cabildo"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert entry["environment"]["CABILDO_CLIENT"] == "opencode"
    assert entry["environment"]["CABILDO_PROJECT"] == "cabildo"
    assert entry["environment"]["CABILDO_HOME"] == "/opt/cabildo-home"


def test_apply_is_idempotent_and_leaves_other_servers_alone(opencode_home):
    oc.config_path().parent.mkdir(parents=True)
    oc.config_path().write_text(json.dumps({"mcp": {"theirs": {"command": ["x"]}}}))
    _, what = oc.apply()
    assert what == "added"
    assert oc.apply()[1] == "unchanged"
    data = json.loads(oc.config_path().read_text())
    assert set(data["mcp"]) == {"theirs", "cabildo"}  # unrelated server untouched


def test_remove_takes_out_cabildo_only(opencode_home):
    oc.config_path().parent.mkdir(parents=True)
    oc.config_path().write_text(json.dumps({"mcp": {"theirs": {"command": ["x"]}}}))
    oc.apply()
    p, what = oc.remove()
    assert what == "removed"
    data = json.loads(p.read_text())
    assert set(data["mcp"]) == {"theirs"}  # ours gone, theirs kept


def test_malformed_config_raises_rather_than_clobbering(opencode_home):
    oc.config_path().parent.mkdir(parents=True)
    oc.config_path().write_text("[]")  # valid JSON, wrong shape
    with pytest.raises(ValueError):
        oc.apply()
