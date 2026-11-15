"""GitHub Actions backend for ``cabildo ci``: REST calls over ``urllib``.

A workflow run is shown as a ``ci.Pipeline`` and its jobs as ``ci.Job``, with GitHub's status
and conclusion folded into GitLab's status names, so ``cabildo ci`` prints and exits the same
for both forges.

The token comes from ``GH_TOKEN`` or ``GITHUB_TOKEN``, then the password of the
``api.github.com`` (or ``github.com``) entry in ``~/.netrc``, then ``oauth_token`` in gh's
``hosts.yml``. With no token it falls back to ``gh api``.
"""

from __future__ import annotations

import json
import netrc
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

TIMEOUT_S = 30
_GH_HOSTS = Path.home() / ".config" / "gh" / "hosts.yml"

# GitHub (status, conclusion) folded into GitLab's names; ci.Pipeline.exit reads these
_RUNNING = frozenset({"queued", "in_progress", "requested", "pending"})
_CONCLUSION = {
    "success": "success",
    "failure": "failed",
    "timed_out": "failed",
    "startup_failure": "failed",
    "cancelled": "canceled",
    "skipped": "skipped",
    "neutral": "skipped",
    "action_required": "manual",
    "stale": "canceled",
}

Call = Callable[..., object]  # api(host, endpoint, method="GET", body=None)


class GithubError(RuntimeError):
    """A GitHub call failed: no token, an HTTP error, or a bad response."""


def status_name(status: str, conclusion: str | None) -> str:
    """Return the GitLab-style status for a GitHub run or job."""
    if status in _RUNNING:
        return "running"
    if status == "waiting":
        return "manual"
    return _CONCLUSION.get(conclusion or "", "failed")


def _gh_hosts_token(host: str, path: Path = _GH_HOSTS) -> str | None:
    """Read ``<host>.oauth_token`` from gh's ``hosts.yml`` without a YAML parser."""
    try:
        text = path.read_text()
    except OSError:
        return None
    in_host = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_host = line.strip().rstrip(":") == host
            continue
        m = re.match(r"\s+oauth_token:\s*(\S+)", line)
        if in_host and m:
            return m[1].strip("'\"")
    return None


def _netrc_token(*hosts: str) -> str | None:
    try:
        rc = netrc.netrc(os.environ.get("NETRC"))
    except (OSError, netrc.NetrcParseError):
        return None
    for host in hosts:
        entry = rc.authenticators(host)
        if entry and entry[2]:
            return entry[2]
    return None


def token(host: str = "github.com") -> str | None:
    """Return the GitHub token, or None when none is configured."""
    env = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    return env or _netrc_token(f"api.{host}", host) or _gh_hosts_token(host, _GH_HOSTS)


def _api_base(host: str) -> str:
    return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"


def api(
    host: str, endpoint: str, method: str = "GET", body: dict[str, Any] | None = None
) -> object:
    """Call the GitHub REST API: parsed JSON, or None for an empty (204) answer.

    Raises:
        GithubError: no token and no gh, an HTTP error, or a network error.
    """
    tok = token(host)
    if tok is None:
        return _gh_fallback(host, endpoint, method, body)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_api_base(host)}/{endpoint}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise GithubError(f"GitHub {e.code} on {endpoint}") from e
    except (urllib.error.URLError, OSError) as e:
        raise GithubError(f"GitHub unreachable: {e}") from e
    return json.loads(text) if text.strip() else None


def _gh_fallback(host: str, endpoint: str, method: str, body: dict[str, Any] | None) -> object:
    cmd = ["gh", "api", "--hostname", host, "--method", method, endpoint]
    if body is not None:
        cmd += ["--input", "-"]
    try:
        p = subprocess.run(
            cmd,
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as e:
        raise GithubError(f"no GitHub token for {host} (set GH_TOKEN or ~/.netrc)") from e
    if p.returncode != 0:
        raise GithubError((p.stderr or p.stdout).strip()[:300])
    return json.loads(p.stdout) if p.stdout.strip() else None


def repo_api(path: str) -> str:
    """Return the ``repos/<owner>/<name>`` prefix for a repo path."""
    owner, _, name = path.partition("/")
    return f"repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
