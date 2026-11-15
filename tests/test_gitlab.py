"""GitLab connector: token lookup, the pipeline poller, dedup, and routing."""

from __future__ import annotations

import json

from cabildo import ci_watch, gitlab, store
from tests.conftest import make_session

REPO = ci_watch.GitlabRepo("gitlab.com", "a/b")
KEY = "gitlab.com/a/b"


def _fake(pipelines):
    calls = []

    def call(host, endpoint):
        calls.append(endpoint)
        if "/pipelines?" in endpoint:
            return pipelines
        if endpoint.endswith("/jobs?per_page=100"):
            return [{"id": 7, "status": "failed", "name": "test", "stage": "ci", "web_url": "u"}]
        if endpoint.endswith("/trace"):
            return "boom\n"
        raise AssertionError(endpoint)

    return call, calls


def _pipe(pid, status="failed", sha="abc123"):
    return {"id": pid, "sha": sha, "ref": "main", "status": status, "web_url": f"p/{pid}"}


def _watched():
    return [gitlab.WatchedRepo(REPO, "alpha", "/repo/alpha")]


def test_first_poll_sets_the_cursor_and_reports_nothing(conn):
    call, calls = _fake([_pipe(1)])
    assert gitlab.poll(conn, call=call, now=lambda: 1000.0, repos=_watched()) == 0
    assert calls == []
    assert store.ci_cursor(conn, KEY) == 1000.0


def test_failed_pipeline_goes_to_the_project_channel_once(conn):
    make_session(conn, "B", handle="bob", project="/repo/alpha", project_name="alpha")
    store.set_ci_cursor(conn, KEY, 900.0)
    call, _ = _fake([_pipe(1), _pipe(2, status="success")])
    assert gitlab.poll(conn, call=call, now=lambda: 1000.0, repos=_watched()) == 1
    assert gitlab.poll(conn, call=call, now=lambda: 1060.0, repos=_watched()) == 0  # dedup
    rows = conn.execute("SELECT * FROM messages WHERE from_session='daemon'").fetchall()
    assert len(rows) == 1
    assert rows[0]["topic"] == store.project_topic("alpha")
    assert rows[0]["urgent"] == 1
    assert "boom" in rows[0]["body"]


def test_failed_pipeline_goes_to_the_pusher_as_a_dm(conn):
    make_session(conn, "P", handle="pat", project="/repo/alpha", project_name="alpha")
    conn.execute(
        "INSERT INTO intents(created_at, from_session, action, params, status, result) "
        "VALUES(1, 'P', 'push', '{}', 'done', ?)",
        (json.dumps({"pushed": True, "sha": "abc123"}),),
    )
    store.set_ci_cursor(conn, KEY, 900.0)
    call, _ = _fake([_pipe(1)])
    gitlab.poll(conn, call=call, now=lambda: 1000.0, repos=_watched())
    row = conn.execute("SELECT * FROM messages WHERE from_session='daemon'").fetchone()
    assert row["to_session"] == "P"


def test_an_api_error_is_logged_and_does_not_raise(conn):
    store.set_ci_cursor(conn, KEY, 900.0)

    def boom(_h, _e):
        raise gitlab.GitlabError("GitLab 401")

    logs: list[str] = []
    assert gitlab.poll(conn, call=boom, now=lambda: 1000.0, repos=_watched(), log=logs.append) == 0
    assert any("401" in m for m in logs)


def test_token_from_env_then_file_then_glab_config(tmp_path, monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("CABILDO_GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    cfg = tmp_path / "glab.yml"
    cfg.write_text(
        "git_protocol: ssh\nhosts:\n    other.com:\n        token: nope\n"
        "    gitlab.com:\n        api_host: gitlab.com\n        token: from-glab\n"
    )
    monkeypatch.setattr(gitlab, "_GLAB_CONFIG", cfg)
    assert gitlab._token_glab_config("gitlab.com", cfg) == "from-glab"
    (tmp_path / "gitlab-token").write_text("gitlab.com from-file\n")
    assert gitlab.token("gitlab.com") == "from-file"
    monkeypatch.setenv("GITLAB_TOKEN", "from-env")
    assert gitlab.token("gitlab.com") == "from-env"


def test_watched_repos_dedups_by_remote(conn):
    make_session(conn, "A", handle="a", project="/repo/alpha", project_name="alpha")
    make_session(conn, "B", handle="b", project="/repo/alpha", project_name="alpha")
    got = gitlab.watched_repos(conn, remote_url=lambda _cwd: "git@gitlab.com:a/b.git")
    assert [w.repo for w in got] == [REPO]


def test_a_401_on_the_glab_token_falls_back_to_glab(tmp_path, monkeypatch):
    import urllib.error

    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("CABILDO_GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path))
    cfg = tmp_path / "glab.yml"
    cfg.write_text("hosts:\n    gitlab.com:\n        token: expired\n")
    monkeypatch.setattr(gitlab, "_GLAB_CONFIG", cfg)

    def deny(*_a, **_k):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(gitlab.urllib.request, "urlopen", deny)
    monkeypatch.setattr(gitlab.ci_watch, "_glab", lambda _h, _e, _m="GET": [{"id": 1}])
    assert gitlab.api("gitlab.com", "projects/x/pipelines") == [{"id": 1}]
