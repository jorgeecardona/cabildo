"""Tests cannot push to a network remote (#19-10)."""

import subprocess


def test_git_refuses_network_push(tmp_path):
    """A push to an https remote fails before any network use."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run(
        ["git", "-C", str(tmp_path), "push", "https://example.invalid/x.git", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode != 0
    assert "not allowed" in r.stderr


def test_git_allows_local_bare_remote(tmp_path):
    """Local bare remotes, which tests use, still work."""
    bare, work = tmp_path / "bare.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    g = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run([*g, "push", "-q", str(bare), "main"], check=True)
