"""GitLab CI for the repo you are in: see a pipeline's jobs, start a pipeline, play or retry a job.

``cabildo ci`` and the ``ci_*`` MCP tools call these. Output is plain lines, one per job, so
it pipes into ``grep`` and ``awk``; ``--json`` gives the same data as JSON. All HTTP goes
through ``gitlab.api``, so the token rules are the same as the pipeline poller's.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import IntEnum
from typing import Any

from . import ci_watch, github, gitlab

Call = Callable[..., object]  # gitlab.api(host, endpoint, method="GET")

_RUNNING = frozenset({"created", "waiting_for_resource", "preparing", "pending", "running"})


class Exit(IntEnum):
    """Exit code of ``cabildo ci status``: a script can branch on it."""

    PASSED = 0
    FAILED = 1
    RUNNING = 2
    NONE = 3  # no pipeline, or no GitLab remote


@dataclass(frozen=True)
class Job:
    """One job of a pipeline."""

    id: int
    name: str
    stage: str
    status: str  # GitLab's job status: success, failed, running, manual, ...
    url: str


@dataclass(frozen=True)
class Pipeline:
    """One pipeline and its jobs, in stage order."""

    id: int
    sha: str
    ref: str
    status: str
    url: str
    jobs: tuple[Job, ...]

    @property
    def exit(self) -> Exit:
        """The exit code for this pipeline's status."""
        if self.status == "success":
            return Exit.PASSED
        if self.status in _RUNNING:
            return Exit.RUNNING
        return Exit.FAILED


class CiError(RuntimeError):
    """No GitLab or GitHub remote here, or the forge refused the call."""


def is_github(repo: ci_watch.GitlabRepo) -> bool:
    """Return True when ``repo`` is on GitHub (github.com or a GitHub Enterprise host)."""
    return "github" in repo.host


def repo_here(cwd: str) -> ci_watch.GitlabRepo:
    """Return the forge repo (GitLab or GitHub) of ``cwd``'s ``origin`` remote.

    Raises:
        CiError: ``cwd`` has no ``origin``, or it is not a ``host:owner/name`` remote.
    """
    url = gitlab._remote_url(cwd)
    repo = ci_watch.parse_remote(url) if url else None
    if repo is None:
        raise CiError(f"no GitLab or GitHub origin remote in {cwd}")
    return repo


def _dicts(value: object) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _job(raw: dict[str, Any]) -> Job:
    return Job(
        id=int(raw["id"]),
        name=str(raw.get("name", "")),
        stage=str(raw.get("stage", "")),
        status=str(raw.get("status", "")),
        url=str(raw.get("web_url", "")),
    )


def _call(call: Call, repo: ci_watch.GitlabRepo, endpoint: str, method: str = "GET") -> object:
    try:
        return call(repo.host, f"{repo.api}/{endpoint}", method)
    except gitlab.GitlabError as e:
        raise CiError(str(e)) from e


def _gh(
    call: Call | None,
    repo: ci_watch.GitlabRepo,
    endpoint: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> object:
    try:
        return (call or github.api)(
            repo.host, f"{github.repo_api(repo.path)}/{endpoint}", method, body
        )
    except github.GithubError as e:
        raise CiError(str(e)) from e


def _gh_pipeline(
    repo: ci_watch.GitlabRepo,
    sha: str | None,
    ref: str | None,
    run_id: int | None,
    call: Call | None,
) -> Pipeline | None:
    if run_id is None:
        query = {
            "per_page": "1",
            **({"head_sha": sha} if sha else {}),
            **({"branch": ref} if ref else {}),
        }
        found = _gh(call, repo, f"actions/runs?{urllib.parse.urlencode(query)}")
        runs = _dicts(found.get("workflow_runs") if isinstance(found, dict) else None)
        if not runs:
            return None
        run_id = int(runs[0]["id"])
    raw = _gh(call, repo, f"actions/runs/{run_id}")
    if not isinstance(raw, dict):
        return None
    listed = _gh(call, repo, f"actions/runs/{run_id}/jobs?per_page=100")
    jobs = _dicts(listed.get("jobs") if isinstance(listed, dict) else None)
    workflow = str(raw.get("name", ""))
    return Pipeline(
        id=run_id,
        sha=str(raw.get("head_sha", "")),
        ref=str(raw.get("head_branch", "")),
        status=github.status_name(str(raw.get("status", "")), raw.get("conclusion")),
        url=str(raw.get("html_url", "")),
        jobs=tuple(
            sorted(
                (
                    Job(
                        id=int(j["id"]),
                        name=str(j.get("name", "")),
                        stage=workflow,
                        status=github.status_name(str(j.get("status", "")), j.get("conclusion")),
                        url=str(j.get("html_url", "")),
                    )
                    for j in jobs
                ),
                key=lambda j: j.id,
            )
        ),
    )


def pipeline(
    repo: ci_watch.GitlabRepo,
    *,
    sha: str | None = None,
    ref: str | None = None,
    pipeline_id: int | None = None,
    call: Call | None = None,
) -> Pipeline | None:
    """Return the newest pipeline (GitHub: workflow run) for ``sha`` or ``ref``, or by id."""
    if is_github(repo):
        return _gh_pipeline(repo, sha, ref, pipeline_id, call)
    call = call or gitlab.api
    if pipeline_id is None:
        query = {"per_page": "1", **({"sha": sha} if sha else {}), **({"ref": ref} if ref else {})}
        found = _dicts(_call(call, repo, f"pipelines?{urllib.parse.urlencode(query)}"))
        if not found:
            return None
        pipeline_id = int(found[0]["id"])
    raw = _call(call, repo, f"pipelines/{pipeline_id}")
    if not isinstance(raw, dict):
        return None
    jobs = _dicts(
        _call(call, repo, f"pipelines/{pipeline_id}/jobs?per_page=100&include_retried=false")
    )
    return Pipeline(
        id=pipeline_id,
        sha=str(raw.get("sha", "")),
        ref=str(raw.get("ref", "")),
        status=str(raw.get("status", "")),
        url=str(raw.get("web_url", "")),
        jobs=tuple(sorted((_job(j) for j in jobs), key=lambda j: j.id)),
    )


def run(
    repo: ci_watch.GitlabRepo,
    ref: str,
    *,
    workflow: str | None = None,
    call: Call | None = None,
) -> Pipeline:
    """Start a new pipeline on ``ref`` and return it.

    On GitHub this dispatches ``workflow`` (file name such as ``ci.yml``, or its id); the
    workflow needs a ``workflow_dispatch`` trigger.
    """
    if is_github(repo):
        if not workflow:
            raise CiError("GitHub needs the workflow to run, for example: ci run ci.yml")
        answer = _gh(
            call,
            repo,
            f"actions/workflows/{urllib.parse.quote(workflow)}/dispatches",
            "POST",
            {"ref": ref, "return_run_details": True},
        )
        run_id = answer.get("workflow_run_id") if isinstance(answer, dict) else None
        found = _gh_pipeline(repo, None, None if run_id else ref, run_id, call)
        if found is None:
            raise CiError(f"dispatched {workflow} on {ref}, but no run is listed yet")
        return found
    call = call or gitlab.api
    raw = _call(call, repo, f"pipeline?{urllib.parse.urlencode({'ref': ref})}", "POST")
    if not isinstance(raw, dict):
        raise CiError("GitLab did not return the new pipeline")
    found = pipeline(repo, pipeline_id=int(raw["id"]), call=call)
    if found is None:
        raise CiError(f"pipeline {raw['id']} vanished")
    return found


def play(repo: ci_watch.GitlabRepo, job_id: int, *, call: Call | None = None) -> Job:
    """Start a manual job and return it.

    GitHub has no manual jobs: ``job_id`` is a workflow run id, and every deployment of that
    run that waits for a reviewer is approved.
    """
    if is_github(repo):
        pending = _dicts(_gh(call, repo, f"actions/runs/{job_id}/pending_deployments"))
        envs = [
            int(p["environment"]["id"]) for p in pending if isinstance(p.get("environment"), dict)
        ]
        if not envs:
            raise CiError(f"run {job_id} has no deployment waiting for approval")
        _gh(
            call,
            repo,
            f"actions/runs/{job_id}/pending_deployments",
            "POST",
            {"environment_ids": envs, "state": "approved", "comment": "approved with cabildo"},
        )
        return Job(id=job_id, name="deployment", stage="", status="running", url="")
    call = call or gitlab.api
    raw = _call(call, repo, f"jobs/{job_id}/play", "POST")
    if not isinstance(raw, dict):
        raise CiError(f"GitLab did not return job {job_id}")
    return _job(raw)


def retry(repo: ci_watch.GitlabRepo, job_id: int, *, call: Call | None = None) -> Job:
    """Run a finished job again and return the new job (on GitHub: the same job id)."""
    if is_github(repo):
        _gh(call, repo, f"actions/jobs/{job_id}/rerun", "POST")
        return Job(id=job_id, name="", stage="", status="running", url="")
    call = call or gitlab.api
    raw = _call(call, repo, f"jobs/{job_id}/retry", "POST")
    if not isinstance(raw, dict):
        raise CiError(f"GitLab did not return a retry of job {job_id}")
    return _job(raw)


def find_job(p: Pipeline, name_or_id: str) -> Job | None:
    """Return the job of ``p`` whose id or name is ``name_or_id``."""
    for job in p.jobs:
        if name_or_id in (str(job.id), job.name):
            return job
    return None


def lines(p: Pipeline) -> str:
    """Render ``p`` as tab-separated lines: a header line, then ``status stage name id`` per job."""
    head = f"pipeline\t{p.status}\t{p.ref}\t{p.sha[:8]}\t{p.id}\t{p.url}"
    rows = [f"{j.status}\t{j.stage}\t{j.name}\t{j.id}" for j in p.jobs]
    return "\n".join([head, *rows])


def as_dict(p: Pipeline) -> dict[str, Any]:
    """Return ``p`` as plain data for ``--json`` and the MCP tools."""
    return asdict(p)
