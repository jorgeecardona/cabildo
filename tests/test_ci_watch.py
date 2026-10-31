"""The daemon reports a failed GitLab pipeline, with the failed job log tail, to the pusher."""

from __future__ import annotations

from cabildo import ci_watch

SHA = "a" * 40
REPO = ci_watch.GitlabRepo("gitlab.com", "cardona-oss/cabildo")


def fake_glab(pipeline_status: str, calls: list[str] | None = None):
    def glab(host: str, endpoint: str) -> object:
        if calls is not None:
            calls.append(endpoint)
        if "/pipelines?sha=" in endpoint:
            return [{"id": 7, "status": pipeline_status, "web_url": "https://x/p/7"}]
        if endpoint.endswith("/pipelines/7/jobs?per_page=100"):
            return [
                {"id": 1, "name": "lint", "stage": "check", "status": "success"},
                {"id": 2, "name": "test", "stage": "check", "status": "failed", "web_url": "u2"},
            ]
        if endpoint.endswith("/jobs/2/trace"):
            return "\x1b[31mline1\x1b[0m\r\n\nFAILED test_x\n"
        raise AssertionError(endpoint)

    return glab


def run_watch(status: str, url: str = "git@gitlab.com:cardona-oss/cabildo.git") -> str | None:
    return ci_watch.watch(
        url, SHA, timeout=100, glab=fake_glab(status), sleep=lambda _s: None, now=lambda: 0.0
    )


def test_parse_remote_forms() -> None:
    assert ci_watch.parse_remote("git@gitlab.com:cardona-oss/cabildo.git") == REPO
    assert ci_watch.parse_remote("https://gitlab.com/cardona-oss/cabildo.git") == REPO
    assert ci_watch.parse_remote("") is None


def test_api_prefix_encodes_the_path() -> None:
    assert REPO.api == "projects/cardona-oss%2Fcabildo"


def test_clean_tail_strips_ansi_and_keeps_last_lines() -> None:
    assert ci_watch.clean_tail("\x1b[31ma\x1b[0m\r\n\nb\nc\n", lines=2) == "b\nc"


def test_failed_pipeline_message_has_job_tail() -> None:
    body = run_watch("failed")
    assert body is not None
    assert "pipeline failed for " + SHA[:12] in body
    assert "job `test` (stage check) u2" in body
    assert "FAILED test_x" in body
    assert "lint" not in body


def test_passed_pipeline_says_nothing() -> None:
    assert run_watch("success") is None


def test_non_gitlab_remote_says_nothing() -> None:
    assert run_watch("failed", url="/some/local/path") is None


def test_glab_error_says_nothing() -> None:
    def boom(_h: str, _e: str) -> object:
        raise RuntimeError("401")

    out = ci_watch.watch("git@gitlab.com:a/b.git", SHA, timeout=1, glab=boom, sleep=lambda _s: None)
    assert out is None


def test_no_pipeline_gives_up_after_the_appear_window() -> None:
    clock = iter(range(0, 10_000, 60))
    result = ci_watch.wait_for_pipeline(
        REPO,
        SHA,
        timeout=10_000,
        glab=lambda _h, _e: [],
        sleep=lambda _s: None,
        now=lambda: float(next(clock)),
    )
    assert result is None


def test_running_pipeline_is_polled_until_final() -> None:
    states = iter(["running", "running", "failed"])

    def glab(host: str, endpoint: str) -> object:
        if "/pipelines?sha=" in endpoint:
            return [{"id": 7, "status": next(states), "web_url": "w"}]
        return fake_glab("failed")(host, endpoint)

    sleeps: list[float] = []
    result = ci_watch.wait_for_pipeline(
        REPO, SHA, timeout=1000, glab=glab, sleep=sleeps.append, now=lambda: 0.0
    )
    assert result is not None and result.status == "failed" and len(sleeps) == 2
