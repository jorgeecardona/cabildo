"""The SessionStart banner, and its WIP nudge when a project gets crowded."""

from __future__ import annotations

from cabildo import notify, store
from tests.conftest import make_session

P = "/repo/x"


def _peers(conn, n):
    for i in range(n):
        make_session(conn, f"peer{i}", handle=f"peer{i}", project=P, project_name="x")


def test_no_wip_nudge_below_the_threshold(conn):
    make_session(conn, "me", handle="me", project=P, project_name="x")
    _peers(conn, notify.WIP_NUDGE_PEERS - 1)
    assert "Claim your task first" not in notify.session_banner(conn, "me")


def test_wip_nudge_when_the_project_is_crowded(conn):
    make_session(conn, "me", handle="me", project=P, project_name="x")
    _peers(conn, notify.WIP_NUDGE_PEERS)
    banner = notify.session_banner(conn, "me")
    assert "Claim your task first" in banner
    assert f"{notify.WIP_NUDGE_PEERS + 1} agents live" in banner


def test_no_nudge_when_alone(conn):
    make_session(conn, "solo", handle="solo", project=P, project_name="x")
    banner = notify.session_banner(conn, "solo")
    assert "Claim your task first" not in banner
    assert "No other agents" in banner


def test_shared_repo_banner_offers_push_by_default(conn):
    """A push-mode project (the default): with peers, the banner routes pushes to
    a push and states push does not touch local main.
    """
    make_session(conn, "me", handle="me", project=P, project_name="x")
    _peers(conn, 1)
    banner = notify.session_banner(conn, "me")
    assert 'request(action="push"' in banner
    assert "does not touch your local main" in banner


def test_shared_repo_banner_drops_push_in_manual_mode(conn, monkeypatch):
    """A manual-mode project pushes by hand: the banner must NOT tell agents to
    queue a push — that path is off, and the guard does not deny the push.
    """
    from cabildo import config

    monkeypatch.setattr(config, "push_mode", lambda *a, **k: "manual")
    make_session(conn, "me", handle="me", project=P, project_name="x")
    _peers(conn, 1)
    banner = notify.session_banner(conn, "me")
    assert 'request(action="push"' not in banner
    assert "Pushing is manual" in banner
    assert "`git push origin main`" in banner
    assert "do not `git push`" not in banner


def test_timeline_text_injects_once_then_throttles(conn):
    make_session(conn, "me", handle="me", project=P, project_name="x")
    first = notify.timeline_text(conn, "me", 600)
    assert first is not None and first.startswith("🕐 ")
    # Second call inside the interval is silent (store.timeline_due throttles it).
    assert notify.timeline_text(conn, "me", 600) is None


def test_timeline_text_off_when_interval_not_positive(conn):
    make_session(conn, "me", handle="me", project=P, project_name="x")
    assert notify.timeline_text(conn, "me", 0) is None


def test_heartbeat_reason_fires_once_then_throttles(conn):
    make_session(conn, "me", handle="me", project=P, project_name="x")
    reason = notify.heartbeat_reason(conn, "me", 600)
    assert reason is not None
    assert "cabildo heartbeat" in reason and reason.startswith("🕐 ")
    # One-shot within the interval: a second stop attempt is not re-blocked here.
    assert notify.heartbeat_reason(conn, "me", 600) is None


def test_heartbeat_reason_off_by_default_interval(conn):
    make_session(conn, "me", handle="me", project=P, project_name="x")
    # heartbeat_s defaults to 0 (off): no nudge, session idles normally.
    assert notify.heartbeat_reason(conn, "me", 0) is None


def test_banner_injects_the_role_description_verbatim(conn):
    make_session(conn, "pm", handle="pm", project=P, project_name="x")
    store.set_role(conn, "pm", "product", "Owns needs. Writes no code. Ask backend.")
    banner = notify.session_banner(conn, "pm")
    assert "Your role: product" in banner
    assert "Owns needs. Writes no code. Ask backend." in banner


def test_banner_has_no_role_line_without_a_role(conn):
    make_session(conn, "dev", handle="dev", project=P, project_name="x")
    assert "Your role" not in notify.session_banner(conn, "dev")
