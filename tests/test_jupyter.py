"""jupyter.py: specs from config, state files, and cells over a fake kernel channel."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cabildo import jupyter
from tests.wsserver import frame, read_frame

CONFIG = """
[jupyter]
packages = ["numpy"]
python = "3.12"

[jupyter.runners.lab]
cwd = "/var/empty"
packages = ["pandas"]
kernelspec = "python3"
startup = "import math"

[jupyter.runners.lab.env]
FOO = "bar"
"""


@pytest.fixture(autouse=True)
def _home(home: Path) -> Path:
    return home


def test_resolve_merges_the_runner_over_the_defaults_and_the_session_cwd_wins(
    home: Path, tmp_path: Path
) -> None:
    (home / "config.toml").write_text(CONFIG)
    lab = jupyter.resolve("lab")
    assert (lab.cwd, lab.packages, lab.python, lab.startup) == (
        Path("/var/empty").resolve(), ("pandas",), "3.12", "import math"
    )  # fmt: skip
    assert lab.env == (("FOO", "bar"),)
    other = jupyter.resolve("lab", str(tmp_path))
    assert other.cwd == tmp_path.resolve()
    ad_hoc = jupyter.resolve("adhoc", str(tmp_path))
    assert (ad_hoc.packages, ad_hoc.python) == (("numpy",), "3.12")  # the [jupyter] defaults
    assert jupyter.named_runners() == ["lab"]


@pytest.mark.parametrize("name", ["", "Up", "a b", "../x", "x" * 40, "-x"])
def test_a_bad_runner_name_is_refused(name: str) -> None:
    with pytest.raises(jupyter.RunnerError, match="runner name"):
        jupyter.resolve(name)


def test_command_builds_a_uv_environment_with_the_spec(tmp_path: Path) -> None:
    spec = jupyter.RunnerSpec("a", tmp_path, python="3.13", packages=("polars",))
    cmd = jupyter.command(spec, 4321)
    assert cmd[:3] == ["uv", "run", "--no-project"]
    assert "jupyterlab" in cmd  # the browser link is JupyterLab at /lab
    assert cmd[cmd.index("polars") - 1 : cmd.index("polars") + 1] == ["--with", "polars"]
    assert cmd[cmd.index("--python") + 1] == "3.13"
    assert "--port=4321" in cmd and f"--ServerApp.root_dir={tmp_path}" in cmd
    assert "--no-browser" in cmd and "--ip=127.0.0.1" in cmd


def test_clip_keeps_the_head_and_the_tail() -> None:
    assert jupyter.clip("short") == ("short", False)
    out, cut = jupyter.clip("a" * 100 + "z" * 100, 40)
    assert cut and out.startswith("a" * 20) and out.endswith("z" * 20) and "160 characters" in out


def test_a_state_whose_process_is_gone_is_removed(home: Path) -> None:
    folder = jupyter.runner_dir("ghost")
    folder.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    jupyter._write_state(jupyter.RunnerState("ghost", dead.pid, 1, "/var/empty", 0.0))
    assert jupyter.running("ghost") is None
    assert not (folder / "state.json").exists()


def test_stop_ends_the_process_group_and_forgets_the_state(home: Path) -> None:
    folder = jupyter.runner_dir("sleepy")
    folder.mkdir(parents=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    jupyter._write_state(jupyter.RunnerState("sleepy", proc.pid, 1, "/var/empty", 0.0))
    assert jupyter.stop("sleepy") is True
    assert proc.wait(timeout=5) != 0
    assert jupyter.stop("sleepy") is False


def test_start_without_uv_or_a_directory_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(jupyter.RunnerError, match="uv is not on PATH"):
        jupyter.start(jupyter.RunnerSpec("a", tmp_path))
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/uv")
    with pytest.raises(jupyter.RunnerError, match="not a directory"):
        jupyter.start(jupyter.RunnerSpec("a", tmp_path / "missing"))


def _fake_running(name: str, port: int, monkeypatch: pytest.MonkeyPatch) -> jupyter.RunnerState:
    """A runner whose server is a fake: ``running`` says yes, the HTTP calls are scripted."""
    folder = jupyter.runner_dir(name)
    folder.mkdir(parents=True)
    (folder / "token").write_text("tok")
    state = jupyter.RunnerState(name, os.getpid(), port, "/var/empty", 0.0, kernel_id="k1")
    jupyter._write_state(state)
    calls: list[tuple[str, str]] = []

    def http(_state, method, path, body=None, timeout=10.0):
        calls.append((method, path))
        return {"id": "k1"}

    monkeypatch.setattr(jupyter, "_http", http)
    monkeypatch.setattr(jupyter, "http_calls", calls, raising=False)
    return state


def _kernel_server(serve, script):
    """A fake channel: on an execute_request, reply with ``script(parent_id)`` messages."""

    def handler(conn: socket.socket, headers: dict[str, str]) -> None:
        _, data = read_frame(conn)
        parent = json.loads(data)["header"]["msg_id"]
        for kind, content in script:
            msg = {"header": {"msg_type": kind}, "parent_header": {"msg_id": parent},
                   "content": content}  # fmt: skip
            conn.sendall(frame(0x1, json.dumps(msg).encode()))
        try:
            conn.recv(10)
        except OSError:
            pass

    return serve(handler)


def test_run_collects_stream_result_and_error(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _kernel_server(serve, [
        ("status", {"execution_state": "busy"}),
        ("stream", {"name": "stdout", "text": "hi\n"}),
        ("execute_result", {"data": {"text/plain": "42"}}),
        ("display_data", {"data": {"image/png": "AAAA", "text/plain": "<Figure>"}}),
        ("error", {"ename": "ValueError", "evalue": "bad", "traceback": ["\x1b[31mTrace\x1b[0m", "last"]}),
        ("execute_reply", {"status": "error"}),
        ("status", {"execution_state": "idle"}),
    ])  # fmt: skip
    state = _fake_running("r1", int(url.split(":")[2].split("/")[0]), monkeypatch)
    result = jupyter.run("r1", "code", timeout=5)
    assert (result.status, result.text, result.images) == ("error", "hi\n42\n", 1)
    assert result.error.startswith("ValueError: bad") and "\x1b" not in result.error
    assert state.kernel_id == "k1"


def test_a_cell_that_runs_too_long_is_interrupted(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _kernel_server(serve, [("stream", {"name": "stdout", "text": "partial"})])
    _fake_running("r2", int(url.split(":")[2].split("/")[0]), monkeypatch)
    result = jupyter.run("r2", "sleep", timeout=0.5)
    assert (result.status, result.text) == ("timeout", "partial")
    assert ("POST", "/api/kernels/k1/interrupt") in jupyter.http_calls  # type: ignore[attr-defined]


def test_run_on_a_runner_that_is_not_running_says_so() -> None:
    with pytest.raises(jupyter.RunnerError, match="not running"):
        jupyter.run("nobody", "1")


def test_open_link_hides_the_token_and_the_page_is_private(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_running("r3", 5555, monkeypatch)
    found = jupyter.open_link("r3", "sub dir/a.ipynb")
    assert found["url"] == "http://127.0.0.1:5555/lab/tree/sub%20dir/a.ipynb"
    assert "token=" not in json.dumps(found)
    page = Path(found["link"].removeprefix("file://"))
    assert "token=tok" in page.read_text() and "5555/lab/tree/sub%20dir/a.ipynb" in page.read_text()
    assert stat.S_IMODE(page.stat().st_mode) == 0o600


@pytest.mark.skipif(
    not os.environ.get("CABILDO_TEST_JUPYTER") or shutil.which("uv") is None,
    reason="starts a real Jupyter Server with uv (network, a minute): set CABILDO_TEST_JUPYTER=1",
)
def test_a_real_runner_runs_cells_and_keeps_variables(tmp_path: Path) -> None:
    state = jupyter.start(jupyter.RunnerSpec("real", tmp_path))
    try:
        assert Path(state.cwd) == tmp_path
        assert jupyter.run("real", "x = 6 * 7").status == "ok"
        assert jupyter.run("real", "print(x)").text == "42\n"
        assert jupyter.run("real", "import os; print(os.getcwd())").text.strip() == str(tmp_path)
    finally:
        jupyter.stop("real")


def _proc_state(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs Linux /proc")
def test_pause_stops_the_tree_and_resume_runs_it_again(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = jupyter.runner_dir("nap")
    folder.mkdir(parents=True)
    (folder / "token").write_text("tok")
    shell = subprocess.Popen(
        ["sh", "-c", "sleep 60 & wait"], start_new_session=True
    )  # a server with a child
    try:
        jupyter._write_state(jupyter.RunnerState("nap", shell.pid, 1, str(home), 0.0))
        monkeypatch.setattr(jupyter, "_http", lambda *a, **k: {})  # the server "answers"
        child = None
        for _ in range(50):
            kids = jupyter._descendants(shell.pid)
            if kids:
                child = kids[0]
                break
            time.sleep(0.05)
        assert child is not None
        assert jupyter.pause("nap") is True and jupyter.pause("nap") is False
        for _ in range(50):
            if _proc_state(shell.pid) == "T" and _proc_state(child) == "T":
                break
            time.sleep(0.05)
        assert (_proc_state(shell.pid), _proc_state(child)) == ("T", "T")
        monkeypatch.setattr(jupyter, "_http", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        assert jupyter.running("nap") is not None  # a stopped server cannot answer: still listed
        assert [r.paused for r in jupyter.statuses() if r.name == "nap"] == [True]
        with pytest.raises(jupyter.RunnerError, match="paused"):
            jupyter.run("nap", "1")
        assert jupyter.resume("nap") is True and jupyter.resume("nap") is False
        for _ in range(50):
            if _proc_state(shell.pid) != "T":
                break
            time.sleep(0.05)
        assert _proc_state(shell.pid) != "T"
    finally:
        jupyter.stop("nap")
        shell.wait(timeout=5)
