"""about.py: the facts about this cabildo, with each service up or down."""

from __future__ import annotations

import json
from importlib import metadata

import pytest

from cabildo import __version__, about, daemon, mcp_server


@pytest.fixture
def no_services(tmp_path, monkeypatch):
    """No daemon info file, no broker, no host: everything reads as down."""
    monkeypatch.setenv("CABILDO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CABILDO_BROKER_SOCKET", str(tmp_path / "none.sock"))
    monkeypatch.setenv("CABILDO_HOST_SOCKET", str(tmp_path / "none-host.sock"))
    (tmp_path / "home").mkdir()


def test_with_nothing_running_every_service_reads_as_down(no_services):
    got = about.gather()
    assert got.version == __version__
    assert got.daemon is None and got.host_process is None
    assert (got.broker.live, got.host.live, got.host_sessions) == (False, False, 0)
    assert got.restart == ()


def test_the_docs_topics_are_listed(no_services):
    assert "start" in about.gather().docs


def test_an_older_daemon_is_reported_with_the_command_that_fixes_it(no_services, monkeypatch):
    installed = about._installed()
    if not installed:
        pytest.skip("cabildo is not installed in this environment")
    daemon.info_path().write_text(json.dumps({"version": "0.0.1", "pid": 42, "exe": "/x/python"}))
    got = about.gather()
    assert got.daemon == about.Running("0.0.1", 42, "/x/python")
    (hint,) = got.restart
    assert "daemon 0.0.1" in hint and "systemctl --user restart cabildo-daemon.service" in hint


def test_the_host_build_and_session_count_come_from_the_host(session_host, tmp_path, monkeypatch):
    from cabildo import host_client

    monkeypatch.setenv("CABILDO_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    host_client.start_session(["sh", "-c", "read x"], cwd=str(tmp_path))
    got = about.gather()
    assert got.host.live and got.host_sessions == 1
    assert got.host_process is not None and got.host_process.version == __version__


def test_the_tool_returns_json(no_services):
    out = mcp_server.about()
    json.dumps(out)
    assert out["version"] == __version__
    assert set(out) >= {"installed", "daemon", "broker", "host", "docs", "restart"}


def test_installed_is_empty_when_the_package_has_no_metadata(monkeypatch):
    def missing(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", missing)
    assert about._installed() == ""
