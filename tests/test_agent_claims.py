"""BusClaims takes file claims through the real store and refuses another session's."""

import subprocess
from pathlib import Path

from cabildo import store
from cabildo.agent.claims import BusClaims


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("x")
    return repo


def test_second_agent_is_refused_until_first_releases(home, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = BusClaims(repo, session_id="local-1")
    second = BusClaims(repo, session_id="local-2")
    assert first.acquire(repo / "a.txt") is None
    assert second.acquire(repo / "a.txt") is not None
    first.release_all()
    assert second.acquire(repo / "a.txt") is None


def test_glob_claim_by_another_session_covers_the_file(home, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    agent = BusClaims(repo, session_id="local-1")
    conn = store.connect()
    store.register_session(conn, "other", cwd=str(repo), project=agent.root, project_name="r")
    store.acquire_claim(
        conn,
        resource="file:*.txt",
        holder_session="other",
        holder_handle=None,
        project=agent.root,
        ttl_seconds=60,
        note=None,
    )
    conn.close()
    assert agent.acquire(repo / "a.txt") is not None
