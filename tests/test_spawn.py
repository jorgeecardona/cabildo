"""Spawn ledger + guardrails: lineage lives in the DB, and every limit is
enforced at the daemon's single choke point.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from cabildo import actions, store
from cabildo.store import HUMAN_SESSION


def _ctx(**kw) -> actions.Ctx:
    return actions.Ctx(worker="test", **kw)


def _task():
    return "audit the gate script and make silent skips fail loudly"


# --------------------------------------------------------------------------- #
# Ledger mechanics
# --------------------------------------------------------------------------- #
def test_register_session_binds_spawn_row(conn):
    store.record_spawn(
        conn, tag="t1", parent_session=HUMAN_SESSION, role="qa", task=_task(), depth=1
    )
    store.register_session(conn, "child-1", cwd="/w", tag="t1")
    row = conn.execute("SELECT * FROM spawns WHERE tag='t1'").fetchone()
    assert row["session_id"] == "child-1"
    assert row["status"] == "registered"
    assert store.spawn_depth_of(conn, "child-1") == 1


def test_end_session_closes_ledger_row(conn):
    store.record_spawn(
        conn, tag="t1", parent_session=HUMAN_SESSION, role="qa", task=_task(), depth=1
    )
    store.register_session(conn, "child-1", cwd="/w", tag="t1")
    store.end_session(conn, "child-1")
    row = conn.execute("SELECT * FROM spawns WHERE tag='t1'").fetchone()
    assert row["status"] == "ended"
    assert row["ended_at"] is not None


def test_depth_is_read_from_lineage_not_claimed(conn):
    assert store.spawn_depth_of(conn, HUMAN_SESSION) == 0
    assert store.spawn_depth_of(conn, None) == 0
    store.record_spawn(
        conn, tag="t1", parent_session=HUMAN_SESSION, role="pm", task=_task(), depth=1
    )
    store.register_session(conn, "child-1", cwd="/w", tag="t1")
    store.record_spawn(
        conn,
        tag="t2",
        parent_session="child-1",
        role="qa",
        task=_task(),
        depth=store.spawn_depth_of(conn, "child-1") + 1,
    )
    store.register_session(conn, "child-2", cwd="/w", tag="t2")
    assert store.spawn_depth_of(conn, "child-2") == 2


def test_reconcile_marks_dead_pids(home):
    conn = store.connect()
    store.record_spawn(
        conn,
        tag="dead",
        parent_session=HUMAN_SESSION,
        role="qa",
        task=_task(),
        depth=1,
        pid=2**22 + 12345,
    )
    store.record_spawn(
        conn,
        tag="alive",
        parent_session=HUMAN_SESSION,
        role="qa",
        task=_task(),
        depth=1,
        pid=os.getpid(),
    )
    assert actions._reconcile_live(conn) == 1
    assert conn.execute("SELECT status FROM spawns WHERE tag='dead'").fetchone()["status"] == "dead"
    assert (
        conn.execute("SELECT status FROM spawns WHERE tag='alive'").fetchone()["status"]
        == "launched"
    )
    conn.close()


# --------------------------------------------------------------------------- #
# Guardrails at the choke point
# --------------------------------------------------------------------------- #
def test_spawn_refuses_beyond_max_depth(home, conn, tmp_path):
    store.record_spawn(
        conn, tag="t1", parent_session=HUMAN_SESSION, role="pm", task=_task(), depth=1
    )
    store.register_session(conn, "child-1", cwd="/w", tag="t1")
    store.record_spawn(conn, tag="t2", parent_session="child-1", role="qa", task=_task(), depth=2)
    store.register_session(conn, "child-2", cwd="/w", tag="t2")

    out = actions.spawn(
        {"role": "qa", "task": _task(), "repo": str(tmp_path), "_from_session": "child-2"},
        _ctx(max_depth=2),
    )
    assert out["ok"] is False
    assert "depth" in out["reason"]


def test_spawn_refuses_at_fleet_cap(home, conn, tmp_path):
    for i in range(2):
        store.record_spawn(
            conn,
            tag=f"t{i}",
            parent_session=HUMAN_SESSION,
            role="qa",
            task=_task(),
            depth=1,
            pid=os.getpid(),
        )
    out = actions.spawn(
        {"role": "qa", "task": _task(), "repo": str(tmp_path), "_from_session": HUMAN_SESSION},
        _ctx(max_agents=2),
    )
    assert out["ok"] is False
    assert "cap" in out["reason"]


def test_spawn_rejects_unknown_model(home, tmp_path):
    with pytest.raises(actions.Reject, match="model"):
        actions.spawn(
            {
                "role": "qa",
                "task": _task(),
                "repo": str(tmp_path),
                "model": "gpt-5",
                "_from_session": HUMAN_SESSION,
            },
            _ctx(),
        )


# --------------------------------------------------------------------------- #
# The launch path
# --------------------------------------------------------------------------- #
def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def test_dry_run_builds_and_removes_worktree(home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(actions.shutil, "which", lambda _: "/bin/true")
    out = actions.spawn(
        {"role": "qa", "task": _task(), "repo": str(repo), "_from_session": HUMAN_SESSION},
        _ctx(allow_spawn=False),
    )
    assert out["ok"] is True and out["spawned"] is False
    assert "dry run" in out["reason"]
    # No ledger row for a dry run, and the worktree is gone.
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) AS n FROM spawns").fetchone()["n"] == 0
    conn.close()
    wt_root = tmp_path / actions.WORKTREE_DIRNAME
    assert not wt_root.exists() or not any(wt_root.iterdir())


def test_launch_records_lineage_and_model(home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = store.connect()
    store.register_session(conn, HUMAN_SESSION, cwd="/home/jorge")
    store.set_handle(conn, HUMAN_SESSION, "jorge")

    captured = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    real_popen = subprocess.Popen

    def fake_popen(cmd, **kw):
        if cmd and cmd[0] == "/bin/true":  # the faked claude binary only —
            captured["cmd"] = cmd  # git still runs for real underneath
            captured["env"] = kw.get("env") or {}
            return FakeProc()
        return real_popen(cmd, **kw)

    monkeypatch.setattr(actions.shutil, "which", lambda _: "/bin/true")
    monkeypatch.setattr(actions.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        actions,
        "_verify_spawn",
        lambda proc, wt, ctx, *, tag, timeout=25: (True, "sess-child", "registered"),
    )

    out = actions.spawn(
        {
            "role": "qa",
            "task": _task(),
            "repo": str(repo),
            "model": "sonnet",
            "_from_session": HUMAN_SESSION,
        },
        _ctx(allow_spawn=True),
    )
    assert out["ok"] is True and out["spawned"] is True
    assert out["depth"] == 1 and out["model"] == "sonnet"

    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "spawned by @jorge" in prompt  # report-back convention is stated
    assert 'request(action="spawn"' in prompt  # and so is the right way to delegate

    tag = captured["env"]["CABILDO_TAG"]
    row = conn.execute("SELECT * FROM spawns WHERE tag=?", (tag,)).fetchone()
    assert row is not None
    assert row["parent_session"] == HUMAN_SESSION
    assert row["depth"] == 1
    assert row["model"] == "sonnet"
    assert row["pid"] == 4242
    assert row["status"] == "launched"
    conn.close()
