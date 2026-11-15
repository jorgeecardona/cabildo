"""``cabildo ci`` on GitHub Actions: runs as pipelines, dispatch, rerun, deployment approval."""

from __future__ import annotations

from typing import Any

import pytest

from cabildo import ci, github
from cabildo.ci_watch import GitlabRepo

REPO = GitlabRepo("github.com", "o/r")
PFX = "repos/o/r/"


class FakeGithub:
    """Answers the Actions endpoints ``ci`` calls and records every call."""

    def __init__(self, status: str = "completed", conclusion: str | None = "success") -> None:
        self.run = {"status": status, "conclusion": conclusion}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(
        self, host: str, endpoint: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> object:
        assert host == "github.com"
        assert endpoint.startswith(PFX)
        path = endpoint.removeprefix(PFX)
        self.calls.append((method, path, body))
        if path.startswith("actions/runs?"):
            return {"workflow_runs": [{"id": 5}]}
        if path == "actions/runs/5":
            return {
                "id": 5,
                "name": "CI",
                "head_sha": "deadbeef99",
                "head_branch": "main",
                "html_url": "https://gh/5",
                **self.run,
            }
        if path.startswith("actions/runs/5/jobs"):
            return {
                "jobs": [
                    {"id": 9, "name": "test", "status": "completed", "conclusion": "failure"},
                    {"id": 8, "name": "lint", "status": "in_progress", "conclusion": None},
                ]
            }
        if path.endswith("/dispatches"):
            return {"workflow_run_id": 5}
        if path == "actions/runs/5/pending_deployments" and method == "GET":
            return [{"environment": {"id": 41, "name": "prod"}}]
        if path == "actions/runs/5/pending_deployments":
            return []
        if path.endswith("/rerun"):
            return None
        raise AssertionError(path)


@pytest.mark.parametrize(
    ("status", "conclusion", "name", "code"),
    [
        ("completed", "success", "success", ci.Exit.PASSED),
        ("completed", "failure", "failed", ci.Exit.FAILED),
        ("completed", "timed_out", "failed", ci.Exit.FAILED),
        ("completed", "cancelled", "canceled", ci.Exit.FAILED),
        ("in_progress", None, "running", ci.Exit.RUNNING),
        ("queued", None, "running", ci.Exit.RUNNING),
    ],
)
def test_run_status_maps_to_gitlab_names(
    status: str, conclusion: str | None, name: str, code: ci.Exit
) -> None:
    p = ci.pipeline(REPO, sha="deadbeef", call=FakeGithub(status, conclusion))
    assert p is not None
    assert p.status == name
    assert p.exit is code


def test_waiting_for_approval_is_manual() -> None:
    assert github.status_name("waiting", None) == "manual"


def test_jobs_are_listed_with_the_workflow_as_stage() -> None:
    p = ci.pipeline(REPO, ref="main", call=FakeGithub())
    assert p is not None
    assert ci.lines(p).splitlines()[1:] == ["running\tCI\tlint\t8", "failed\tCI\ttest\t9"]


def test_run_dispatches_the_workflow_and_returns_its_run() -> None:
    fake = FakeGithub()
    p = ci.run(REPO, "main", workflow="ci.yml", call=fake)
    assert p.id == 5
    assert (
        "POST",
        "actions/workflows/ci.yml/dispatches",
        {"ref": "main", "return_run_details": True},
    ) in fake.calls


def test_run_without_a_workflow_is_refused() -> None:
    with pytest.raises(ci.CiError, match="workflow"):
        ci.run(REPO, "main", call=FakeGithub())


def test_play_approves_the_waiting_deployments() -> None:
    fake = FakeGithub()
    ci.play(REPO, 5, call=fake)
    posts = [(p, b) for m, p, b in fake.calls if m == "POST"]
    assert posts == [
        (
            "actions/runs/5/pending_deployments",
            {"environment_ids": [41], "state": "approved", "comment": "approved with cabildo"},
        )
    ]


def test_retry_reruns_the_job() -> None:
    fake = FakeGithub()
    ci.retry(REPO, 9, call=fake)
    assert ("POST", "actions/jobs/9/rerun", None) in fake.calls


def test_token_order_env_then_netrc_then_gh_hosts(tmp_path, monkeypatch) -> None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    hosts = tmp_path / "hosts.yml"
    hosts.write_text("github.com:\n    user: me\n    oauth_token: from-gh\n")
    monkeypatch.setattr(github, "_GH_HOSTS", hosts)
    assert github.token() == "from-gh"
    rc = tmp_path / "netrc"
    rc.write_text("machine api.github.com login me password from-netrc\n")
    rc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(rc))
    assert github.token() == "from-netrc"
    monkeypatch.setenv("GH_TOKEN", "from-env")
    assert github.token() == "from-env"
