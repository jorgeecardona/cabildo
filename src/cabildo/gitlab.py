"""Native GitLab connector: REST calls over ``urllib`` and a pipeline poller.

The daemon calls ``poll`` on a timer. For every GitLab repo that an active session
works in, it lists the pipelines updated since the last poll. A pipeline that
ended ``failed`` is reported once: as an urgent DM to the agent that pushed the
commit through cabildo, else as an urgent post to the project channel. Both wake
an idle agent through the push plugin.

The token comes from ``CABILDO_GITLAB_TOKEN`` or ``GITLAB_TOKEN``, then the
password of the host's ``~/.netrc`` entry (or ``$NETRC``), then
``~/.cabildo/gitlab-token``, then glab's config file (read as a file; the ``glab``
binary is only a last resort when no token is found).
"""

from __future__ import annotations

import json
import netrc
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import ci_watch, store

Api = Callable[[str, str], object]
TIMEOUT_S = 30
MAX_PIPELINES = 50
_GLAB_CONFIG = Path.home() / ".config" / "glab-cli" / "config.yml"


class GitlabError(RuntimeError):
    """A GitLab call failed: no token, HTTP error, or a bad response."""


@dataclass(frozen=True)
class Pipeline:
    """One pipeline as the list endpoint returns it."""

    id: int
    sha: str
    ref: str
    status: str
    url: str
    updated_at: str


@dataclass(frozen=True)
class WatchedRepo:
    """A GitLab repo and the cabildo project whose sessions work in it."""

    repo: ci_watch.GitlabRepo
    project_name: str
    project: str | None


# --------------------------------------------------------------------------- #
# Token + HTTP
# --------------------------------------------------------------------------- #
def _cabildo_home() -> Path:
    return Path(os.environ.get("CABILDO_HOME") or Path.home() / ".cabildo")


def _token_file(host: str) -> str | None:
    """Read ``~/.cabildo/gitlab-token``: one bare token, or ``<host> <token>`` lines."""
    try:
        lines = (_cabildo_home() / "gitlab-token").read_text().splitlines()
    except OSError:
        return None
    rows = [ln.split() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    for row in rows:
        if len(row) == 2 and row[0] == host:
            return row[1]
    singles = [row[0] for row in rows if len(row) == 1]
    return singles[0] if singles else None


def _token_glab_config(host: str, path: Path = _GLAB_CONFIG) -> str | None:
    """Read ``hosts.<host>.token`` from glab's ``config.yml`` without a YAML parser."""
    try:
        text = path.read_text()
    except OSError:
        return None
    in_hosts = False
    in_host = False
    host_indent = -1
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key = line.strip()
        if indent == 0:
            in_hosts = key == "hosts:"
            in_host = False
            continue
        if not in_hosts:
            continue
        if key.rstrip(":") == host and key.endswith(":"):
            in_host, host_indent = True, indent
            continue
        if in_host and indent <= host_indent:
            in_host = False
        if in_host:
            m = re.match(r"token:\s*(\S+)", key)
            if m and not m[1].startswith("!!"):
                return m[1].strip("'\"")
    return None


def _token_netrc(host: str) -> str | None:
    """Return the password of ``host`` in ``$NETRC`` or ``~/.netrc``, or None."""
    try:
        entry = netrc.netrc(os.environ.get("NETRC")).authenticators(host)
    except (OSError, netrc.NetrcParseError):
        return None
    return entry[2] if entry and entry[2] else None


def _own_token(host: str) -> str | None:
    """A token the user set: the environment, ``~/.netrc``, or ``~/.cabildo/gitlab-token``."""
    env = os.environ.get("CABILDO_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN")
    return env or _token_netrc(host) or _token_file(host)


def token(host: str) -> str | None:
    """Return the GitLab token for ``host``, or None when none is configured."""
    return _own_token(host) or _token_glab_config(host)


def api(host: str, endpoint: str, method: str = "GET") -> object:
    """Call ``https://<host>/api/v4/<endpoint>``: parsed JSON, or text for a job trace.

    ``method`` is ``GET`` or ``POST``; parameters of a POST go in the endpoint's query
    string. With no token it falls back to ``glab api`` if glab is installed.

    Raises:
        GitlabError: no token and no glab, an HTTP error, or a network error.
    """
    tok = token(host)
    if tok is None:
        return _glab_fallback(host, endpoint, method)
    # glab's OAuth2 token expires every 2 hours and only glab refreshes it, so a
    # 401 on that token goes through ``glab api``, which refreshes and retries.
    from_glab = tok == _token_glab_config(host) and not _own_token(host)
    req = urllib.request.Request(
        f"https://{host}/api/v4/{endpoint}",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 401 and from_glab:
            return _glab_fallback(host, endpoint, method)
        raise GitlabError(f"GitLab {e.code} on {endpoint}") from e
    except (urllib.error.URLError, OSError) as e:
        raise GitlabError(f"GitLab unreachable: {e}") from e
    if endpoint.endswith("/trace"):
        return body
    try:
        return json.loads(body)
    except ValueError as e:
        raise GitlabError(f"GitLab returned non-JSON for {endpoint}") from e


def _glab_fallback(host: str, endpoint: str, method: str = "GET") -> object:
    try:
        return ci_watch._glab(host, endpoint, method)
    except FileNotFoundError as e:
        msg = f"no GitLab token for {host} (set GITLAB_TOKEN or ~/.cabildo/gitlab-token)"
        raise GitlabError(msg) from e
    except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
        raise GitlabError(str(e)) from e


# --------------------------------------------------------------------------- #
# Poller
# --------------------------------------------------------------------------- #
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _remote_url(cwd: str) -> str | None:
    try:
        p = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def watched_repos(
    conn: sqlite3.Connection, remote_url: Callable[[str], str | None] = _remote_url
) -> list[WatchedRepo]:
    """The GitLab repos of the projects that have an active session, one per repo."""
    seen: dict[ci_watch.GitlabRepo, WatchedRepo] = {}
    for s in store.active_sessions(conn):
        name, cwd = s["project_name"], s["cwd"]
        if not name or not cwd:
            continue
        url = remote_url(str(cwd))
        repo = ci_watch.parse_remote(url) if url else None
        if repo is not None and repo not in seen:
            seen[repo] = WatchedRepo(repo, str(name), s["project"])
    return list(seen.values())


def _pipelines(repo: ci_watch.GitlabRepo, since: float, call: Api) -> list[Pipeline]:
    raw = call(
        repo.host,
        f"{repo.api}/pipelines?updated_after={_iso(since)}"
        f"&order_by=updated_at&sort=asc&per_page={MAX_PIPELINES}",
    )
    rows = raw if isinstance(raw, list) else []
    return [
        Pipeline(
            id=int(r["id"]),
            sha=str(r.get("sha", "")),
            ref=str(r.get("ref", "")),
            status=str(r.get("status", "")),
            url=str(r.get("web_url", "")),
            updated_at=str(r.get("updated_at", "")),
        )
        for r in rows
        if isinstance(r, dict) and "id" in r
    ]


def pusher_of(conn: sqlite3.Connection, sha: str) -> str | None:
    """The session that pushed ``sha`` through a cabildo ``push`` intent, if any."""
    for row in conn.execute(
        "SELECT from_session, result FROM intents WHERE action='push' AND status='done' "
        "ORDER BY id DESC LIMIT 200"
    ):
        try:
            result = json.loads(row["result"] or "{}")
        except ValueError:
            continue
        if result.get("pushed") is True and str(result.get("sha") or "") == sha:
            return str(row["from_session"])
    return None


def poll(
    conn: sqlite3.Connection,
    *,
    call: Api = api,
    now: Callable[[], float] = time.time,
    repos: list[WatchedRepo] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> int:
    """Report every newly failed pipeline of the watched repos. Returns how many were reported.

    A repo seen for the first time starts its cursor at ``now``: old failures are
    not replayed. Errors on one repo are logged and do not stop the others.
    """
    reported = 0
    for w in watched_repos(conn) if repos is None else repos:
        key = f"{w.repo.host}/{w.repo.path}"
        since = store.ci_cursor(conn, key)
        t = now()
        if since is None:
            store.set_ci_cursor(conn, key, t)
            continue
        try:
            pipelines = _pipelines(w.repo, since, call)
        except GitlabError as e:
            log(f"gitlab: {key}: {e}")
            continue
        for p in pipelines:
            if p.status != "failed" or not store.record_ci_event(conn, key, p.id, p.status):
                continue
            try:
                jobs = ci_watch.failed_jobs(w.repo, p.id, call)
            except GitlabError:
                jobs = ()
            body = ci_watch.message(p.sha, ci_watch.PipelineResult(p.id, p.status, p.url, jobs))
            body += f"\n(ref {p.ref}, {key})"
            _deliver(conn, w, p, body)
            reported += 1
            log(f"gitlab: reported failed pipeline {p.id} of {key}")
        # A little overlap: a pipeline updated in the same second is seen twice
        # and dropped by record_ci_event, rather than missed.
        store.set_ci_cursor(conn, key, t - 5)
    return reported


def _deliver(conn: sqlite3.Connection, w: WatchedRepo, p: Pipeline, body: str) -> None:
    from .daemon import DAEMON_HANDLE, DAEMON_SESSION  # local: daemon imports this module

    who = pusher_of(conn, p.sha)
    store.post_message(
        conn,
        from_session=DAEMON_SESSION,
        from_handle=DAEMON_HANDLE,
        body=body,
        to_session=who,
        topic=None if who else store.project_topic(w.project_name),
        project=w.project,
        urgent=True,
    )
