"""Codex MCP registration wiring (#9): the `codex mcp add` command and the
`config.toml` block we generate. Pure builders — no Codex install required.
"""

from __future__ import annotations

import pytest

# New module: guard so the land daemon's gate (which imports the stale main
# checkout) skips this file instead of erroring. CI on the branch is authoritative.
codex_config = pytest.importorskip("cabildo.codex_config")

from cabildo.mcp_config import mcp_command


def test_add_argv_registers_stdio_server_with_env_then_command():
    argv = codex_config.add_argv(project="alpha", home="/home/j/.cabildo")
    assert argv[:4] == ["codex", "mcp", "add", "cabildo"]
    # env flags come before the `--` separator; the launch command after it.
    sep = argv.index("--")
    head, cmd = argv[:sep], argv[sep + 1 :]
    assert cmd == mcp_command()
    assert "--env" in head
    joined = " ".join(head)
    assert "CABILDO_CLIENT=codex" in joined
    assert "CABILDO_HOME=/home/j/.cabildo" in joined
    assert "CABILDO_PROJECT=alpha" in joined


def test_no_static_session_id_is_ever_written():
    # A static CABILDO_SESSION_ID would collapse every Codex session onto one
    # bus identity; #6's owner-pid path handles Codex identity instead.
    argv = codex_config.add_argv(project="alpha", home="/h")
    assert not any("CABILDO_SESSION_ID" in tok for tok in argv)
    assert "CABILDO_SESSION_ID" not in codex_config.toml_block(project="alpha", home="/h")


def test_server_env_omits_unset_optionals():
    assert codex_config.server_env() == {"CABILDO_CLIENT": "codex"}
    env = codex_config.server_env(project="beta", home="/h")
    assert env == {"CABILDO_CLIENT": "codex", "CABILDO_HOME": "/h", "CABILDO_PROJECT": "beta"}


def test_toml_block_has_both_tables():
    block = codex_config.toml_block(project="alpha", home="/h")
    assert "[mcp_servers.cabildo]" in block
    assert "command = " in block
    assert "args = [" in block
    assert "[mcp_servers.cabildo.env]" in block
    assert 'CABILDO_CLIENT = "codex"' in block
    assert block.endswith("\n")


def test_output_is_idempotent():
    # Same inputs -> byte-identical command and block, so re-running never churns.
    a1 = codex_config.add_argv(project="alpha", home="/h")
    a2 = codex_config.add_argv(project="alpha", home="/h")
    assert a1 == a2
    assert codex_config.toml_block(project="alpha", home="/h") == codex_config.toml_block(
        project="alpha", home="/h"
    )


def test_toml_strings_are_escaped():
    # A home with a quote/backslash must not break the TOML.
    block = codex_config.toml_block(home='/weird/"q\\x')
    assert r'CABILDO_HOME = "/weird/\"q\\x"' in block


def test_remove_argv():
    assert codex_config.remove_argv() == ["codex", "mcp", "remove", "cabildo"]
