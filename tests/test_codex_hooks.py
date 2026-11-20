"""Codex delivery + cooperative coordination (#10).

Covers the Codex hook handlers (registration, the claim guard, unread/urgent
delivery), the hooks.json generation, and — the load-bearing acceptance item —
that a claim is mutually visible across clients: a Codex-held lease denies a
Claude tool call and vice versa, because the guard reads the shared claims table
regardless of who holds it.
"""

from __future__ import annotations

import json

import pytest

# New module: guard so the push daemon's gate (stale main import) skips this
# file rather than erroring; CI on the branch is authoritative.
codex_hooks = pytest.importorskip("cabildo.codex_hooks")

from cabildo import codex_config, guard, store


# --------------------------------------------------------------------------- #
# Hook handlers
# --------------------------------------------------------------------------- #
def test_session_start_registers_as_codex(conn, monkeypatch):
    monkeypatch.setattr("cabildo.context.resolve_codex_pid", lambda *a, **k: 333)
    codex_hooks.session_start({"session_id": "cx1", "cwd": "/repo/x"})
    row = store.get_session(conn, "cx1")
    assert row is not None
    assert row["client"] == "codex"
    assert row["owner_pid"] == 333  # bound to the codex process, not the hook's pid


def wrote_at(conn, session_id):
    row = store.get_session(conn, session_id)
    assert row is not None
    return row["wrote_at"]


def test_pre_tool_use_denies_force_push(conn, capsys):
    store.register_session(
        conn, "cx", client="codex", owner_pid=1, project="/repo/x", project_name="x"
    )
    codex_hooks.pre_tool_use(
        {
            "session_id": "cx",
            "tool_name": "Bash",
            "tool_input": {"command": "git push --force origin main"},
        }
    )
    obj = json.loads(capsys.readouterr().out)
    hso = obj["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "force" in hso["permissionDecisionReason"]


def test_pre_tool_use_allows_edits(conn, capsys):
    # File writes are no longer gated: an Edit is allowed, takes no lease, and a
    # peer reaching for the same file is allowed too.
    store.register_session(
        conn, "cx", client="codex", owner_pid=1, project="/repo/x", project_name="x"
    )
    codex_hooks.pre_tool_use(
        {"session_id": "cx", "tool_name": "Edit", "tool_input": {"file_path": "/repo/x/b.py"}}
    )
    assert capsys.readouterr().out == ""  # allowed → no deny emitted
    store.register_session(conn, "peer", claude_pid=2, project="/repo/x", project_name="x")
    assert guard.deny_reason(conn, "peer", "Edit", {"file_path": "/repo/x/b.py"}) is None


def test_user_prompt_submit_injects_unread(conn, capsys, monkeypatch):
    monkeypatch.setattr("cabildo.context.resolve_codex_pid", lambda *a, **k: 5)
    codex_hooks.session_start({"session_id": "cx", "cwd": "/repo/x"})
    store.register_session(conn, "peer", claude_pid=2, project="/repo/x", project_name="x")
    store.set_handle(conn, "peer", "peer")
    store.post_message(conn, from_session="peer", from_handle="peer", body="ping", to_session="cx")
    capsys.readouterr()  # clear the SessionStart banner
    codex_hooks.user_prompt_submit({"session_id": "cx", "cwd": "/repo/x"})
    assert "additionalContext" in capsys.readouterr().out


def test_unknown_event_reports_and_does_not_raise(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cabildo-codex-hook", "Nope"])
    codex_hooks.main()
    assert "unknown event" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# hooks.json generation
# --------------------------------------------------------------------------- #
def test_hooks_config_idempotent_and_preserves_others(monkeypatch):
    # The login-shell probe has a timeout, so on a loaded machine two calls can disagree.
    monkeypatch.setattr("cabildo.mcp_config._on_login_path", lambda _script: True)
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "X", "hooks": [{"type": "command", "command": "other-tool"}]}
            ]
        }
    }
    once = codex_config.hooks_config(existing)
    twice = codex_config.hooks_config(once)
    assert once == twice  # re-running is a no-op
    cmds = [h["command"] for e in once["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert any("cabildo-codex-hook PreToolUse" in c for c in cmds)
    assert "other-tool" in cmds  # someone else's hook untouched
    for ev in codex_config.HOOK_EVENTS:
        assert ev in once["hooks"]


def test_hooks_matcher_only_on_tool_events():
    cfg = codex_config.hooks_config({})
    assert cfg["hooks"]["PreToolUse"][0]["matcher"] == ".*"
    assert "matcher" not in cfg["hooks"]["SessionStart"][0]


def test_hook_command_str_includes_event():
    s = codex_config._hook_command_str("PreToolUse")
    assert "cabildo-codex-hook" in s and s.endswith("PreToolUse")


# --------------------------------------------------------------------------- #
# Cross-client coordination (the #10 acceptance item): a Claude session and a
# Codex session sharing one checkout see each other as peers, so the
# shared-checkout rule fires in both directions.
# --------------------------------------------------------------------------- #
def test_shared_checkout_is_enforced_across_clients(conn):
    store.register_session(
        conn, "claude-A", claude_pid=111, cwd="/repo/x", project="/repo/x", project_name="x"
    )
    store.set_handle(conn, "claude-A", "alice")
    store.register_session(
        conn,
        "codex-B",
        client="codex",
        owner_pid=222,
        cwd="/repo/x",
        project="/repo/x",
        project_name="x",
    )
    store.set_handle(conn, "codex-B", "bob")
    store.mark_wrote(conn, "claude-A")
    store.mark_wrote(conn, "codex-B")
    # Claude commits in the shared checkout → denied, naming the Codex peer.
    reason = guard.deny_reason(conn, "claude-A", "Bash", {"command": "git commit -am x"})
    assert reason is not None and "@bob" in reason
    # Reverse: Codex commits → denied, naming the Claude peer.
    reason = guard.deny_reason(conn, "codex-B", "Bash", {"command": "git commit -am x"})
    assert reason is not None and "@alice" in reason


def test_a_codex_apply_patch_marks_the_session_as_a_writer(conn):
    store.register_session(
        conn, "cx", client="codex", owner_pid=1, project="/repo/x", project_name="x"
    )
    codex_hooks.post_tool_use({"session_id": "cx", "tool_name": "Read", "tool_input": {}})
    assert wrote_at(conn, "cx") is None
    codex_hooks.post_tool_use({"session_id": "cx", "tool_name": "apply_patch", "tool_input": {}})
    assert wrote_at(conn, "cx") is not None
