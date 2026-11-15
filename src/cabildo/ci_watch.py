"""Watch the GitLab pipeline of a pushed commit and report failed jobs.

The daemon calls ``watch`` after a push is verified on the remote. ``watch`` waits for
the pipeline of that commit to finish. If it failed, it returns a message with the tail
of each failed job log, so the agent that pushed can fix it without the human copying
logs. All GitLab access goes through ``glab api``, which already holds the login.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

TAIL_LINES = 40
MAX_FAILED_JOBS = 3
POLL_SECONDS = 20.0
# The pipeline for a commit appears a few seconds after the push, not at once.
APPEAR_SECONDS = 120.0
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")
_REMOTE = re.compile(
    r"^(?:git@|ssh://git@|https?://)(?P<host>[^:/]+)[:/](?P<path>.+?)(?:\.git)?/?$"
)
_FINAL = frozenset({"success", "failed", "canceled", "skipped"})
Glab = Callable[[str, str], object]


@dataclass(frozen=True)
class FailedJob:
    """One failed job of a pipeline, with the last lines of its log."""

    id: int
    name: str
    stage: str
    url: str
    tail: str


@dataclass(frozen=True)
class PipelineResult:
    """The final state of one pipeline and its failed jobs (empty unless it failed)."""

    id: int
    status: str
    url: str
    failed: tuple[FailedJob, ...]


@dataclass(frozen=True)
class GitlabRepo:
    """Host and full path of a GitLab remote, for example ``gitlab.com`` + ``a/b``."""

    host: str
    path: str

    @property
    def api(self) -> str:
        """The ``projects/<url-encoded path>`` API prefix."""
        return f"projects/{urllib.parse.quote(self.path, safe='')}"


def parse_remote(url: str) -> GitlabRepo | None:
    """Return the repo for a ``git@host:path`` or ``https://host/path`` remote, else None."""
    m = _REMOTE.match(url.strip())
    if m is None:
        return None
    return GitlabRepo(host=m["host"], path=m["path"])


def clean_tail(trace: str, lines: int = TAIL_LINES) -> str:
    """Return the last ``lines`` lines of a job log, without ANSI codes or empty lines."""
    text = _ANSI.sub("", trace)
    kept = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(kept[-lines:])


def _glab(host: str, endpoint: str, method: str = "GET") -> object:
    """Call ``glab api`` and return the parsed JSON, or the raw text for a job trace."""
    p = subprocess.run(
        ["glab", "api", "--hostname", host, "--method", method, endpoint],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300])
    if endpoint.endswith("/trace"):
        return p.stdout
    return json.loads(p.stdout)


def _list(value: object) -> list[dict[str, object]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def failed_jobs(repo: GitlabRepo, pipeline_id: int, glab: Glab) -> tuple[FailedJob, ...]:
    """Fetch the failed jobs of a pipeline and the tail of each log."""
    jobs = _list(glab(repo.host, f"{repo.api}/pipelines/{pipeline_id}/jobs?per_page=100"))
    out: list[FailedJob] = []
    for job in jobs:
        if job.get("status") != "failed":
            continue
        job_id = int(str(job["id"]))
        try:
            tail = clean_tail(str(glab(repo.host, f"{repo.api}/jobs/{job_id}/trace")))
        except RuntimeError as e:
            tail = f"(could not fetch the log: {e})"
        out.append(
            FailedJob(
                id=job_id,
                name=str(job.get("name", "")),
                stage=str(job.get("stage", "")),
                url=str(job.get("web_url", "")),
                tail=tail,
            )
        )
        if len(out) >= MAX_FAILED_JOBS:
            break
    return tuple(out)


def wait_for_pipeline(
    repo: GitlabRepo,
    sha: str,
    *,
    timeout: float,
    glab: Glab = _glab,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> PipelineResult | None:
    """Poll until the pipeline of ``sha`` is final. Returns None when none appears in time."""
    start = now()
    while True:
        pipelines = _list(glab(repo.host, f"{repo.api}/pipelines?sha={sha}&per_page=1"))
        if pipelines:
            p = pipelines[0]
            status = str(p.get("status"))
            if status in _FINAL:
                pid = int(str(p["id"]))
                failed = failed_jobs(repo, pid, glab) if status == "failed" else ()
                return PipelineResult(pid, status, str(p.get("web_url", "")), failed)
        elif now() - start > APPEAR_SECONDS:
            return None
        if now() - start > timeout:
            return None
        sleep(POLL_SECONDS)


def message(sha: str, result: PipelineResult) -> str:
    """Build the bus message for a failed or canceled pipeline."""
    lines = [f"❌ pipeline {result.status} for {sha[:12]}: {result.url}"]
    for job in result.failed:
        lines.append(f"\njob `{job.name}` (stage {job.stage}) {job.url}\n```text\n{job.tail}\n```")
    return "\n".join(lines)


def watch(
    remote_url: str,
    sha: str,
    *,
    timeout: float,
    glab: Glab = _glab,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str | None:
    """Return a failure message for the pipeline of ``sha``, or None when there is nothing to say.

    None means the remote is not GitLab, the pipeline passed, or no pipeline finished in time.
    A glab error is not raised: a broken report must not break the daemon.
    """
    repo = parse_remote(remote_url)
    if repo is None:
        return None
    try:
        result = wait_for_pipeline(repo, sha, timeout=timeout, glab=glab, sleep=sleep, now=now)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        return None  # GitlabError is a RuntimeError
    if result is None or result.status in ("success", "skipped"):
        return None
    return message(sha, result)
