"""Scenario: a session that queues a push on every pass of a loop (#40).

The failure came from a real incident (#39): one worktree queued 15 pushes in a row, each valid
by itself, and the daemon pushed them all. This replays it in a throwaway repo through the daemon,
then checks the end state instead of any single call:

- origin/main has one linear history,
- no commit is lost,
- the number of pushes that landed is capped,
- the pushes after the cap are refused, and the session is told why.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cabildo import actions, daemon, store

from .conftest import make_session

IDENT = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}
PASSES = 15


def sh(cmd: list[str], cwd: Path) -> str:
    p = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, env={**os.environ, **IDENT}, check=False
    )
    assert p.returncode == 0, f"{cmd}: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def make_repos(tmp_path: Path) -> tuple[Path, Path]:
    origin, seed, work = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    sh(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path)
    sh(["git", "init", "-b", "main", str(seed)], tmp_path)
    (seed / "base.txt").write_text("base\n")
    sh(["git", "add", "base.txt"], seed)
    sh(["git", "commit", "-m", "base"], seed)
    sh(["git", "push", str(origin), "main"], seed)
    sh(["git", "clone", str(origin), str(work)], tmp_path)
    return origin, work


def test_a_looping_session_is_capped_and_history_stays_linear(conn, home, tmp_path):
    origin, work = make_repos(tmp_path)
    make_session(conn, "loop", handle="looper", project=str(work), project_name="work")
    ctx = actions.Ctx(worker="w", allow_push=True, log=lambda _m: None)
    results = []
    made: list[str] = []
    for i in range(PASSES):
        (work / f"f{i}.txt").write_text(f"{i}\n")
        sh(["git", "add", f"f{i}.txt"], work)
        sh(["git", "commit", "-m", f"pass {i}"], work)
        made.append(sh(["git", "rev-parse", "HEAD"], work))
        store.request_intent(
            conn,
            from_session="loop",
            from_handle="looper",
            project=str(work),
            action="push",
            params={"cwd": str(work), "target": "main", "_from_session": "loop"},
        )
        intent = store.take_next_intent(conn, "w")
        assert intent is not None
        daemon.handle(conn, intent, ctx)
        row = conn.execute("SELECT * FROM intents WHERE id=?", (intent["id"],)).fetchone()
        results.append(dict(row))

    landed = [r for r in results if r["status"] == "done"]
    refused = [r for r in results if r["status"] != "done"]
    assert len(landed) == 10  # the default limit
    assert len(refused) == PASSES - 10
    assert all("rate limit" in str(r["result"]) for r in refused)

    log = sh(["git", "--git-dir", str(origin), "log", "--format=%H %P", "main"], tmp_path)
    parents = [line.split()[1:] for line in log.splitlines()]
    assert all(len(p) <= 1 for p in parents), "origin/main must stay linear"
    on_origin = {line.split()[0] for line in log.splitlines()}
    assert set(made[:10]) <= on_origin, "every landed commit is on origin/main"
    assert not set(made[10:]) & on_origin, "a refused push put nothing on origin/main"
    # nothing is lost locally either: the refused commits are still in the loop's checkout
    assert sh(["git", "rev-list", "--count", "HEAD"], work) == str(PASSES + 1)

    told = conn.execute(
        "SELECT body FROM messages WHERE to_session='loop' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert told is not None
    assert "rate limit" in told["body"]
