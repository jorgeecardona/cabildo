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

from . import ci_watch, gitlab

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
    """No GitLab remote here, or GitLab refused the call."""


def repo_here(cwd: str) -> ci_watch.GitlabRepo:
    """Return the GitLab repo of ``cwd``'s ``origin`` remote.

    Raises:
        CiError: ``cwd`` has no ``origin``, or it is not a GitLab-style remote.
    """
    url = gitlab._remote_url(cwd)
    repo = ci_watch.parse_remote(url) if url else None
    if repo is None:
        raise CiError(f"no GitLab origin remote in {cwd}")
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


def pipeline(
    repo: ci_watch.GitlabRepo,
    *,
    sha: str | None = None,
    ref: str | None = None,
    pipeline_id: int | None = None,
    call: Call = gitlab.api,
) -> Pipeline | None:
    """Return the newest pipeline for ``sha`` or ``ref`` (or by id) with its jobs, or None."""
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


def run(repo: ci_watch.GitlabRepo, ref: str, *, call: Call = gitlab.api) -> Pipeline:
    """Start a new pipeline on ``ref`` and return it."""
    raw = _call(call, repo, f"pipeline?{urllib.parse.urlencode({'ref': ref})}", "POST")
    if not isinstance(raw, dict):
        raise CiError("GitLab did not return the new pipeline")
    found = pipeline(repo, pipeline_id=int(raw["id"]), call=call)
    if found is None:
        raise CiError(f"pipeline {raw['id']} vanished")
    return found


def play(repo: ci_watch.GitlabRepo, job_id: int, *, call: Call = gitlab.api) -> Job:
    """Start a manual job and return it."""
    raw = _call(call, repo, f"jobs/{job_id}/play", "POST")
    if not isinstance(raw, dict):
        raise CiError(f"GitLab did not return job {job_id}")
    return _job(raw)


def retry(repo: ci_watch.GitlabRepo, job_id: int, *, call: Call = gitlab.api) -> Job:
    """Run a finished job again and return the new job."""
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
