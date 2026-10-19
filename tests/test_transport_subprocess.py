"""Cross-process transport over the shared SQLite bus (#12).

Ported from the removed local prototype. Two independently identified sessions,
each driving the real cabildo MCP server tools in its OWN OS process against a
single shared CABILDO_HOME database, exercise:

  * a project-channel message with an explicit read receipt,
  * direct messages in both directions,
  * a conflicting claim (one process holds a lease, the other is denied, then
    the holder releases and the other succeeds),
  * restart identity — the same CABILDO_SESSION_ID resolving to one identity
    across two separate process launches.

This is a TRANSPORT test, not a live-client smoke test: identity is set through
CABILDO_SESSION_ID / CABILDO_CLIENT env (as each client's hooks would set it)
and the server's tool functions are called directly, rather than over the MCP
stdio JSON-RPC protocol. A real Claude/Codex handoff still needs #9/#10's
manual smoke test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("cabildo.mcp_server")

PROJECT = "transport-demo"


def _call(home, session_id, client, expr: str):
    """Run one MCP tool call as `session_id` in a fresh subprocess and return
    its JSON result. `expr` is evaluated against the server module bound to `s`.
    """
    code = textwrap.dedent(
        f"""
        import json
        from cabildo import mcp_server as s
        print("__RESULT__" + json.dumps({expr}))
        """
    )
    env = dict(os.environ)
    env.update(
        CABILDO_HOME=str(home),
        CABILDO_SESSION_ID=session_id,
        CABILDO_CLIENT=client,
        CABILDO_PROJECT=PROJECT,  # pin both sessions to one project channel
    )
    # A stable cwd unrelated to the test repo; project comes from CABILDO_PROJECT.
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=str(home),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"subprocess failed:\n{proc.stderr}"
    line = next(x for x in proc.stdout.splitlines() if x.startswith("__RESULT__"))
    return json.loads(line[len("__RESULT__") :])


@pytest.fixture
def bus(tmp_path):
    """A shared CABILDO_HOME plus two named identities with handles set (each
    set in its own process, so the sessions are registered cross-process).
    """
    home = tmp_path / "bus"
    home.mkdir()
    _call(home, "alice-sess", "claude", "s.set_handle(handle='alice')")
    _call(home, "bob-sess", "codex", "s.set_handle(handle='bob')")
    return home


def test_peers_discover_each_other_across_processes(bus):
    # list_agents excludes the caller, so each side sees only the other.
    seen_by_alice = {
        a["session_id"]: a for a in _call(bus, "alice-sess", "claude", "s.list_agents()")
    }
    assert seen_by_alice["bob-sess"]["client"] == "codex"
    seen_by_bob = {a["session_id"]: a for a in _call(bus, "bob-sess", "codex", "s.list_agents()")}
    assert seen_by_bob["alice-sess"]["client"] == "claude"


def test_channel_message_with_read_receipt(bus):
    _call(bus, "alice-sess", "claude", "s.send(body='standup in 5')")
    box = _call(bus, "bob-sess", "codex", "s.inbox(unread_only=True)")["messages"]
    assert len(box) == 1 and box[0]["from"] == "@alice"
    r = _call(bus, "bob-sess", "codex", f"s.read(message_ids=[{box[0]['id']!r}])")
    assert r["marked_read"] == 1 and r["unread_remaining"] == 0


def test_direct_messages_both_directions(bus):
    # codex -> claude
    assert _call(bus, "bob-sess", "codex", "s.send(body='take the parser?', to='alice')")["ok"]
    box = _call(bus, "alice-sess", "claude", "s.inbox(unread_only=True)")["messages"]
    assert len(box) == 1 and box[0]["from"] == "@bob"
    _call(bus, "alice-sess", "claude", f"s.read(message_ids=[{box[0]['id']!r}])")

    # claude -> codex
    assert _call(bus, "alice-sess", "claude", "s.send(body='yes, on it', to='bob')")["ok"]
    box = _call(bus, "bob-sess", "codex", "s.inbox(unread_only=True)")["messages"]
    assert len(box) == 1 and box[0]["from"] == "@alice"


def test_conflicting_claim_across_processes(bus):
    got = _call(bus, "alice-sess", "claude", "s.claim(resource='file:x.py', ttl_seconds=300)")
    assert got["ok"] is True

    denied = _call(bus, "bob-sess", "codex", "s.claim(resource='file:x.py', ttl_seconds=300)")
    assert denied["ok"] is False
    assert denied["held_by_session"] == "alice-sess"

    _call(bus, "alice-sess", "claude", "s.release(resource='file:x.py')")
    now = _call(bus, "bob-sess", "codex", "s.claim(resource='file:x.py', ttl_seconds=300)")
    assert now["ok"] is True


def test_restart_identity_is_stable(bus):
    # A second, separate process launched with the same session id resolves to
    # the same identity (not a fresh provisional one).
    first = _call(bus, "alice-sess", "claude", "s.whoami()")
    second = _call(bus, "alice-sess", "claude", "s.whoami()")
    assert first["session_id"] == second["session_id"] == "alice-sess"
