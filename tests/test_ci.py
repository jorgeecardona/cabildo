"""``cabildo ci``: GitLab pipeline status, run, play and retry, plus the netrc token."""

from __future__ import annotations

from typing import Any

import pytest

from cabildo import ci, gitlab, mcp_server
from cabildo.ci_watch import GitlabRepo

REPO = GitlabRepo("gitlab.com", "g/p")
PFX = "projects/g%2Fp/"


class FakeGitlab:
    """Answers the endpoints ``ci`` calls and records every call."""

    def __init__(self, status: str = "success") -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, host: str, endpoint: str, method: str = "GET") -> object:
        assert host == "gitlab.com"
        assert endpoint.startswith(PFX)
        path = endpoint.removeprefix(PFX)
        self.calls.append((method, path))
        if path.startswith("pipelines?"):
            return [{"id": 7}]
        if path == "pipelines/7":
            return {
                "id": 7,
                "sha": "abcdef1234",
                "ref": "main",
                "status": self.status,
                "web_url": "https://x/7",
            }
        if path.startswith("pipelines/7/jobs"):
            return [
                {"id": 12, "name": "deploy", "stage": "deploy", "status": "manual"},
                {"id": 11, "name": "test", "stage": "test", "status": "failed"},
            ]
        if path.startswith("pipeline?"):
            return {"id": 7}
        if path.endswith("/play") or path.endswith("/retry"):
            return {"id": 99, "name": "deploy", "stage": "deploy", "status": "pending"}
        raise AssertionError(path)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("success", ci.Exit.PASSED),
        ("failed", ci.Exit.FAILED),
        ("running", ci.Exit.RUNNING),
        ("pending", ci.Exit.RUNNING),
        ("canceled", ci.Exit.FAILED),
    ],
)
def test_status_exit_code(status: str, code: ci.Exit) -> None:
    p = ci.pipeline(REPO, sha="abc", call=FakeGitlab(status))
    assert p is not None
    assert p.exit is code


def test_status_lines_are_tab_separated_and_jobs_sorted() -> None:
    p = ci.pipeline(REPO, ref="main", call=FakeGitlab())
    assert p is not None
    out = ci.lines(p).splitlines()
    assert out[0].split("\t")[:5] == ["pipeline", "success", "main", "abcdef12", "7"]
    assert out[1] == "failed\ttest\ttest\t11"
    assert out[2] == "manual\tdeploy\tdeploy\t12"


def test_no_pipeline_returns_none() -> None:
    def empty(_h: str, _e: str, _m: str = "GET") -> object:
        return []

    assert ci.pipeline(REPO, sha="abc", call=empty) is None


def test_run_play_retry_post_to_the_right_endpoints() -> None:
    fake = FakeGitlab()
    ci.run(REPO, "main", call=fake)
    ci.play(REPO, 12, call=fake)
    ci.retry(REPO, 11, call=fake)
    posts = [path for method, path in fake.calls if method == "POST"]
    assert posts == ["pipeline?ref=main", "jobs/12/play", "jobs/11/retry"]


def test_find_job_by_name_or_id() -> None:
    p = ci.pipeline(REPO, sha="abc", call=FakeGitlab())
    assert p is not None
    assert ci.find_job(p, "deploy").id == 12  # type: ignore[union-attr]
    assert ci.find_job(p, "11").name == "test"  # type: ignore[union-attr]
    assert ci.find_job(p, "nope") is None


def test_netrc_password_is_the_token(tmp_path, monkeypatch) -> None:
    rc = tmp_path / "netrc"
    rc.write_text("machine gitlab.com login me password glpat-secret\n")
    rc.chmod(0o600)
    for var in ("CABILDO_GITLAB_TOKEN", "GITLAB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NETRC", str(rc))
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    assert gitlab.token("gitlab.com") == "glpat-secret"
    assert gitlab._token_netrc("other.host") is None


def _fake_repo(monkeypatch, fake: FakeGitlab) -> None:
    monkeypatch.setattr(ci, "repo_here", lambda _cwd: REPO)
    for name in ("run", "play"):
        real = getattr(ci, name)
        monkeypatch.setattr(ci, name, lambda *a, _r=real, **_k: _r(*a, call=fake))


def test_mcp_play_needs_the_human(monkeypatch) -> None:
    fake = FakeGitlab()
    _fake_repo(monkeypatch, fake)
    monkeypatch.setattr(mcp_server, "_human_allows", lambda _what: False)
    out: dict[str, Any] = mcp_server.ci_play(12)
    assert out["ok"] is False
    assert not [c for c in fake.calls if c[0] == "POST"]
    monkeypatch.setattr(mcp_server, "_human_allows", lambda _what: True)
    assert mcp_server.ci_play(12)["ok"] is True
    assert ("POST", "jobs/12/play") in fake.calls
