"""The gateway runs inside the daemon, survives a reload, and can be set as the Claude Code base URL."""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from cabildo import daemon, gateway
from tests.test_daemon_reload import ARGS, hup_when

CONFIG = """
[gateway]
port = {port}

[gateway.upstream.subscription]
kind = "passthrough"
base_url = "http://127.0.0.1:9"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def configured(home: Path) -> int:
    port = free_port()
    (home / "config.toml").write_text(CONFIG.format(port=port))
    return port


def status(port: int) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/_cabildo/status")
    return json.loads(conn.getresponse().read())


def test_no_gateway_table_means_no_service(home: Path) -> None:
    assert gateway.start_service(lambda _m: None) is None


def test_a_bad_config_is_logged_and_the_daemon_goes_on(home: Path) -> None:
    (home / "config.toml").write_text("[gateway]\nport = 1\n")
    logged: list[str] = []
    assert gateway.start_service(logged.append) is None
    assert "not started" in logged[0]


def test_a_busy_port_is_logged_and_the_daemon_goes_on(configured: int) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", configured))
        busy.listen()
        logged: list[str] = []
        assert gateway.start_service(logged.append) is None
    assert "cannot listen" in logged[0]


def test_the_service_answers_and_counts_open_connections(configured: int) -> None:
    svc = gateway.start_service(lambda _m: None)
    assert svc is not None
    try:
        assert status(configured)["upstreams"][0]["name"] == "subscription"
        idle = socket.create_connection(("127.0.0.1", configured))
        deadline = time.monotonic() + 5
        while svc.active == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert svc.active == 1
        idle.close()
        deadline = time.monotonic() + 5
        while svc.active and time.monotonic() < deadline:
            time.sleep(0.02)
        assert svc.active == 0
    finally:
        svc.close()


def test_a_paused_service_queues_new_connections_and_a_handoff_serves_them(
    configured: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = gateway.start_service(lambda _m: None)
    assert first is not None
    first.pause()
    waiting = socket.create_connection(("127.0.0.1", configured), timeout=5)  # backlog accepts it
    host = f"Host: 127.0.0.1:{configured}\r\n"
    waiting.sendall(f"GET /_cabildo/status HTTP/1.0\r\n{host}\r\n".encode())
    first.handoff()
    second = gateway.start_service(lambda _m: None)  # the new process takes the same socket
    try:
        assert second is not None
        assert gateway.HANDOFF_ENV not in os.environ
        assert second.port == configured
        data = b""
        while chunk := waiting.recv(4096):
            data += chunk
        assert b"200 OK" in data
        assert b"subscription" in data
    finally:
        waiting.close()
        if second is not None:
            second.close()  # closes the socket both servers wrap: here they share one process


def test_a_handed_over_socket_for_another_port_is_not_used(
    configured: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()
    monkeypatch.setenv(gateway.HANDOFF_ENV, str(os.dup(other.fileno())))
    svc = gateway.start_service(lambda _m: None)
    try:
        assert svc is not None
        assert svc.port == configured
    finally:
        other.close()
        if svc is not None:
            svc.close()


def test_the_daemon_serves_the_gateway_and_hands_the_socket_over_on_reload(
    configured: int,
) -> None:
    seen: list[dict] = []
    ready = threading.Event()

    def probe() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                seen.append(status(configured))
                break
            except OSError:
                time.sleep(0.05)
        ready.set()

    threading.Thread(target=probe, daemon=True).start()
    hup_when(ready)
    try:
        assert daemon.main(ARGS) == daemon.RELOAD_EXIT
        assert seen, "the gateway did not answer while the daemon ran"
        fd = int(os.environ[gateway.HANDOFF_ENV])
        assert socket.socket(fileno=fd).getsockname()[1] == configured
    finally:
        os.environ.pop(gateway.HANDOFF_ENV, None)


def test_enable_writes_the_base_url_and_keeps_the_other_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "x", "env": {"A": "1"}}))
    assert gateway.point_claude_at(path, 8788, None) == []
    data = json.loads(path.read_text())
    assert data["env"] == {"A": "1", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8788"}
    assert data["model"] == "x"
    assert json.loads(path.with_suffix(".json.bak").read_text())["env"] == {"A": "1"}


def test_enable_creates_the_file_and_warns_about_an_api_key_helper(tmp_path: Path) -> None:
    path = tmp_path / "new" / "settings.json"
    assert gateway.point_claude_at(path, 8788, None) == []
    path.write_text(json.dumps({"apiKeyHelper": "echo k"}))
    assert "apiKeyHelper" in gateway.point_claude_at(path, 8788, None)[0]


def test_enable_refuses_to_overwrite_another_base_url(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.com"}}))
    with pytest.raises(gateway.GatewayError, match="already sets"):
        gateway.point_claude_at(path, 8788, None)
    assert "proxy.example.com" in path.read_text()


def test_enable_sets_the_token_header_and_disable_removes_only_ours(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "x-team: a"}}))
    gateway.point_claude_at(path, 8788, "s3cret")
    headers = json.loads(path.read_text())["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert headers == ["x-team: a", "x-cabildo-gateway: s3cret"]
    assert gateway.unpoint_claude(path) is True
    env = json.loads(path.read_text())["env"]
    assert env == {"ANTHROPIC_CUSTOM_HEADERS": "x-team: a"}
    assert gateway.unpoint_claude(path) is False


def test_disable_leaves_a_base_url_that_is_not_local(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.com"}}))
    assert gateway.unpoint_claude(path) is False
    assert "proxy.example.com" in path.read_text()


def test_the_settings_path_follows_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert gateway.claude_settings_path() == tmp_path / "settings.json"
