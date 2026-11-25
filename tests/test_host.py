"""The session host: start, adopt, attach, resize, tag and kill, over a real socket."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import termios
import threading
import time

import pytest

from cabildo import host, host_client


def _viewer(path: str, host_id: str, rows: int = 24, cols: int = 80) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(5)
    sock.connect(path)
    sock.sendall(
        json.dumps({"op": "attach", "id": host_id, "rows": rows, "cols": cols}).encode() + b"\n"
    )
    assert host_client._read_line(sock)["ok"]
    return sock


def _read_until(sock: socket.socket, needle: bytes) -> tuple[bytes, int | None]:
    """Terminal bytes until ``needle`` shows, and the exit code if the session ended first."""
    seen = b""
    while needle not in seen:
        frame = host.read_frame(sock)
        if frame is None:
            return seen, None
        kind, payload = frame
        if kind == host.DATA:
            seen += payload
        elif kind == host.EXIT:
            return seen, host.unpack_code(payload)
    return seen, None


def _type(sock: socket.socket, text: bytes) -> None:
    sock.sendall(host.pack_frame(host.DATA, text))


def test_start_then_attach_types_into_the_session(session_host, tmp_path):
    record = host_client.start_session(
        ["sh", "-c", "echo ready; read x; echo got-$x"], cwd=str(tmp_path)
    )
    sock = _viewer(session_host.path, record.id)
    seen, _ = _read_until(sock, b"ready")
    assert b"ready" in seen
    _type(sock, b"hi\n")
    seen, code = _read_until(sock, b"got-hi")
    assert b"got-hi" in seen
    _, code = _read_until(sock, b"never")
    assert code == 0


def test_two_viewers_see_the_same_output(session_host, tmp_path):
    record = host_client.start_session(["sh", "-c", "read x; echo seen-$x"], cwd=str(tmp_path))
    first, second = _viewer(session_host.path, record.id), _viewer(session_host.path, record.id)
    _type(first, b"both\n")
    assert b"seen-both" in _read_until(first, b"seen-both")[0]
    assert b"seen-both" in _read_until(second, b"seen-both")[0]


def test_a_closed_viewer_leaves_the_session_running(session_host, tmp_path):
    record = host_client.start_session(["sh", "-c", "read x; echo back-$x"], cwd=str(tmp_path))
    _viewer(session_host.path, record.id).close()
    time.sleep(0.2)
    assert [r.id for r in host_client.list_sessions()] == [record.id]
    sock = _viewer(session_host.path, record.id)
    _type(sock, b"again\n")
    assert b"back-again" in _read_until(sock, b"back-again")[0]


def test_size_follows_the_viewer(session_host, tmp_path):
    record = host_client.start_session(
        ["sh", "-c", "read x; stty size"], cwd=str(tmp_path), rows=30, cols=100
    )
    sock = _viewer(session_host.path, record.id, rows=40, cols=120)
    sock.sendall(host.pack_frame(host.RESIZE, host.pack_size(50, 132)))
    time.sleep(0.2)
    _type(sock, b"\n")
    assert b"50 132" in _read_until(sock, b"50 132")[0]


def test_the_environment_and_directory_are_the_requested_ones(session_host, tmp_path):
    record = host_client.start_session(
        ["sh", "-c", "echo $FROM_CLIENT $(pwd); read x"],
        cwd=str(tmp_path),
        env={**os.environ, "FROM_CLIENT": "yes"},
    )
    seen, _ = _read_until(_viewer(session_host.path, record.id), f"yes {tmp_path}".encode())
    assert f"yes {tmp_path}".encode() in seen


def test_adopt_takes_over_a_running_terminal(session_host):
    master, slave = os.openpty()
    proc = subprocess.Popen(
        ["sh", "-c", "read x; echo adopted-$x"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    record = host_client.adopt_session(master, proc.pid, cwd="/x")
    os.close(master)  # the host holds its own copy: this must not hang the child up
    assert record.adopted
    sock = _viewer(session_host.path, record.id)
    _type(sock, b"z\n")
    assert b"adopted-z" in _read_until(sock, b"adopted-z")[0]
    proc.wait(5)


def test_adopt_refuses_a_dead_process(session_host):
    master, slave = os.openpty()
    os.close(slave)
    with pytest.raises(host_client.ClientError, match="not running"):
        host_client.adopt_session(master, 2**22 + 12345)
    os.close(master)


def test_tag_and_list(session_host, tmp_path):
    record = host_client.start_session(["sh", "-c", "read x"], cwd=str(tmp_path), group="g")
    host_client.tag_session(record.id, session_id="abc")
    (listed,) = host_client.list_sessions()
    assert (listed.id, listed.session_id, listed.group) == (record.id, "abc", "g")


def test_kill_ends_the_session(session_host, tmp_path):
    record = host_client.start_session(["sh", "-c", "read x"], cwd=str(tmp_path))
    sock = _viewer(session_host.path, record.id)
    host_client.kill_session(record.id)
    _, code = _read_until(sock, b"never")
    assert code is not None
    deadline = time.monotonic() + 3
    while host_client.list_sessions() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert host_client.list_sessions() == []


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"command": [], "cwd": "/"}, "needs a command"),
        ({"command": ["sh"], "cwd": "/no/such/dir"}, "no such directory"),
        ({"command": ["no-such-binary-xyz"], "cwd": "/"}, "command not found"),
    ],
)
def test_start_refuses_a_bad_request(session_host, params, message):
    with pytest.raises(host_client.ClientError, match=message):
        host_client.request("start", **params)


def test_attach_to_an_unknown_id_is_refused(session_host):
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(session_host.path)
    sock.sendall(b'{"op": "attach", "id": "nope"}\n')
    reply = host_client._read_line(sock)
    assert reply["ok"] is False
    assert "no hosted session" in reply["error"]


def test_a_second_host_on_the_same_socket_refuses(session_host):
    with pytest.raises(host.HostRunning):
        host.Host(session_host.path).serve()


def test_attach_command_relays_a_terminal(session_host, tmp_path):
    record = host_client.start_session(
        ["sh", "-c", "read go; echo ready; read x; echo got-$x"], cwd=str(tmp_path)
    )
    master, slave = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "cabildo.cli", "attach", record.id],
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)

    def read_until(needle: bytes) -> bytes:
        seen = b""
        deadline = time.monotonic() + 10
        while needle not in seen and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    seen += os.read(master, 4096)
                except OSError:
                    break
        return seen

    deadline = time.monotonic() + 10  # attached before the session prints anything
    while not session_host.sessions[record.id].viewers and time.monotonic() < deadline:
        time.sleep(0.02)
    # the viewer registers before `tty.setraw` flushes its input: typing earlier is lost
    while termios.tcgetattr(master)[3] & termios.ICANON and time.monotonic() < deadline:
        time.sleep(0.02)
    os.write(master, b"go\n")
    assert b"ready" in read_until(b"ready")
    os.write(master, b"q\n")
    assert b"got-q" in read_until(b"got-q")
    assert proc.wait(10) == 0
    os.close(master)


def test_the_attach_command_names_the_socket(session_host):
    command = host_client.attach_command("abc")
    assert command[-3:] == ("--socket", session_host.path, "abc")


def test_ping_reports_the_build(session_host):
    from cabildo import __version__

    reply = host_client.ping()
    assert reply is not None
    assert (reply["version"], reply["pid"]) == (__version__, os.getpid())


def test_a_host_whose_socket_file_is_gone_stops(tmp_path):
    path = str(tmp_path / "gone.sock")
    h = host.Host(path)
    thread = threading.Thread(target=h.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.01)
    os.unlink(path)  # as a deleted test directory would
    thread.join(5)
    assert not thread.is_alive()


def test_a_hosted_session_does_not_inherit_the_starting_claudes_markers(session_host, tmp_path):
    env = {
        **os.environ,
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CONFIG_DIR": "/keep/me",
    }
    record = host_client.start_session(
        [
            "sh",
            "-c",
            'echo "[$CLAUDE_CODE_CHILD_SESSION][$CLAUDECODE][$CLAUDE_CONFIG_DIR]"; read x',
        ],
        cwd=str(tmp_path),
        env=env,
    )
    sock = _viewer(session_host.path, record.id)
    seen, _ = _read_until(sock, b"[/keep/me]")
    assert b"[][][/keep/me]" in seen


def test_clean_env_drops_only_the_session_markers():
    got = host_client.clean_env(
        {"CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_USE_BEDROCK": "1", "A": "b"}
    )
    assert got == {"CLAUDE_CODE_USE_BEDROCK": "1", "A": "b"}


def test_stop_is_refused_while_a_session_runs_unless_forced(session_host, tmp_path):
    host_client.start_session(["sh", "-c", "read x"], cwd=str(tmp_path))
    with pytest.raises(host_client.ClientError, match="still run"):
        host_client.stop_host()
    assert host_client.stop_host(force=True) == 1


def test_stop_ends_an_idle_host(tmp_path, monkeypatch):
    path = str(tmp_path / "idle.sock")
    monkeypatch.setenv("CABILDO_HOST_SOCKET", path)
    h = host.Host(path)
    thread = threading.Thread(target=h.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host_client.stop_host() == 0
    thread.join(5)
    assert not thread.is_alive()
