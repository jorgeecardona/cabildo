"""jupyter.py: specs from config, state files, and cells over a fake kernel channel."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cabildo import jupyter
from tests.wsserver import frame, read_frame

# a 3x2 PNG header followed by nothing: enough for the size and the bytes
PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 3, 2) + b"\x08\x02\x00\x00\x00"
).decode()

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
        ("display_data", {"data": {"image/png": PNG_B64, "text/plain": "<Figure>"}}),
        ("error", {"ename": "ValueError", "evalue": "bad", "traceback": ["\x1b[31mTrace\x1b[0m", "last"]}),
        ("execute_reply", {"status": "error", "execution_count": 7}),
        ("status", {"execution_state": "idle"}),
    ])  # fmt: skip
    state = _fake_running("r1", int(url.split(":")[2].split("/")[0]), monkeypatch)
    result = jupyter.run("r1", "code", timeout=5)
    assert (result.status, result.text, len(result.images)) == ("error", "hi\n42\n", 1)
    assert result.images[0].mime == "image/png" and Path(result.images[0].path).read_bytes()
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


def test_a_project_runner_uses_the_projects_environment(home: Path, tmp_path: Path) -> None:
    (home / "config.toml").write_text(f'[jupyter.runners.p]\nproject = "{tmp_path}"\n')
    spec = jupyter.resolve("p", str(tmp_path))
    assert spec.project == tmp_path.resolve()
    cmd = jupyter.command(spec, 1)
    assert cmd[:4] == ["uv", "run", "--project", str(tmp_path.resolve())]
    assert "--no-project" not in cmd and "jupyterlab" in cmd
    with pytest.raises(jupyter.RunnerError, match="not a uv project"):
        jupyter.start(spec)


@pytest.mark.parametrize("bad", ["", "a.py", "../x.ipynb", "a/../../x.ipynb"])
def test_a_notebook_path_must_stay_inside_the_directory(bad: str) -> None:
    with pytest.raises(jupyter.RunnerError, match="inside the runner's directory"):
        jupyter.notebook_path(bad)
    assert jupyter.notebook_path("/sub/a.ipynb") == "sub/a.ipynb"


class FakeServer:
    """Scripted answers for the runner's HTTP calls; records what was asked."""

    def __init__(self, sessions: list[dict], missing: bool = False) -> None:
        self.sessions, self.missing, self.calls = sessions, missing, []

    def __call__(self, _state, method, path, body=None, timeout=10.0):
        self.calls.append((method, path, body))
        written = {c[1] for c in self.calls if c[0] == "PUT"}
        wanted = path.partition("?")[0]
        if method == "GET" and wanted.startswith("/api/contents/") and self.missing:
            if wanted in written:
                return {}
            raise jupyter.RunnerError(f"{method} {path}: HTTP 404")
        if method == "GET" and path == "/api/sessions":
            return self.sessions
        if method == "POST" and path == "/api/sessions":
            return {"kernel": {"id": "k-new"}}
        return {}


def test_a_notebook_with_a_session_runs_on_that_sessions_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = FakeServer([{"path": "a/n.ipynb", "kernel": {"id": "k-lab"}}])
    monkeypatch.setattr(jupyter, "_http", server)
    state = jupyter.RunnerState("r", 1, 1, str(tmp_path), 0.0)
    spec = jupyter.RunnerSpec("r", tmp_path)
    assert jupyter._notebook_kernel(state, spec, "a/n.ipynb") == "k-lab"
    assert not [c for c in server.calls if c[0] in ("POST", "PUT")]  # nothing was made


def test_a_new_notebook_gets_folders_a_file_a_session_and_the_startup_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = FakeServer([], missing=True)
    ran: list[tuple[str, str]] = []
    monkeypatch.setattr(jupyter, "_http", server)
    monkeypatch.setattr(
        jupyter,
        "_execute",
        lambda _s, kernel, code, timeout: ran.append((kernel, code)) or jupyter.RunResult("ok", ""),
    )
    state = jupyter.RunnerState("r", 1, 1, str(tmp_path), 0.0)
    spec = jupyter.RunnerSpec("r", tmp_path, startup="import math")
    assert jupyter._notebook_kernel(state, spec, "a/b/n.ipynb") == "k-new"
    puts = [(c[1], (c[2] or {}).get("type")) for c in server.calls if c[0] == "PUT"]
    assert puts == [
        ("/api/contents/a", "directory"),
        ("/api/contents/a/b", "directory"),
        ("/api/contents/a/b/n.ipynb", "notebook"),
    ]
    posted = next(c for c in server.calls if c[0] == "POST")
    assert posted[2]["path"] == "a/b/n.ipynb" and posted[2]["type"] == "notebook"
    assert ran == [("k-new", "import math")]


def test_a_plot_is_saved_as_a_file_with_its_size(home: Path, monkeypatch) -> None:
    jupyter.runner_dir("o").mkdir(parents=True)
    out = jupyter._Outputs("o", "abcdef123")
    assert out.add({"image/png": PNG_B64, "text/plain": "<Figure>"}) is True
    assert out.add({"image/svg+xml": "<svg/>"}) is True
    assert out.add({"text/html": "<script>Plotly.newPlot()</script>"}) is True
    assert out.add({"text/html": "<table></table>", "text/plain": "df"}) is False  # a dataframe
    assert out.add({"text/plain": "1"}) is False
    png = out.images[0]
    assert (png.mime, png.width, png.height) == ("image/png", 3, 2)
    assert Path(png.path).name == "01.png" and Path(png.path).read_bytes()[:4] == b"\x89PNG"
    assert [Path(i.path).name for i in out.images] == ["01.png", "02.svg"]
    assert [(Path(h.path).name, h.kind) for h in out.html] == [("03.html", "plotly")]


def test_a_cell_that_prints_text_leaves_no_folder(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _kernel_server(serve, [
        ("stream", {"name": "stdout", "text": "x" * 9000}),
        ("execute_reply", {"status": "ok", "execution_count": 3}),
        ("status", {"execution_state": "idle"}),
    ])  # fmt: skip
    _fake_running("r9", int(url.split(":")[2].split("/")[0]), monkeypatch)
    r = jupyter.run("r9", "code", timeout=5)
    assert (r.status, r.truncated, r.execution_count) == ("ok", True, 3)
    assert len(r.text) < 9000 and Path(r.full_text).read_text() == "x" * 9000
    assert Path(r.full_text).parent.parent.name == "out" and not r.images


def test_old_executions_are_pruned(home: Path) -> None:
    out = jupyter.runner_dir("p") / "out"
    for i in range(7):
        (out / f"20260101-00000{i}-aaaaaa").mkdir(parents=True)
    jupyter._prune(out, keep=3)
    assert sorted(p.name for p in out.iterdir()) == [
        "20260101-000004-aaaaaa", "20260101-000005-aaaaaa", "20260101-000006-aaaaaa",
    ]  # fmt: skip


def test_interrupt_and_restart_target_the_runners_or_the_notebooks_kernel(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    server = FakeServer([{"path": "a/n.ipynb", "kernel": {"id": "k-nb"}}])
    monkeypatch.setattr(jupyter, "_http", server)
    state = _fake_running("r8", 1, monkeypatch)  # replaces _http: put the server back
    monkeypatch.setattr(jupyter, "_http", server)
    ran: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jupyter, "_execute",
        lambda _s, kernel, code, timeout: ran.append((kernel, code)) or jupyter.RunResult("ok", ""),
    )  # fmt: skip
    (home / "config.toml").write_text('[jupyter.runners.r8]\nstartup = "import math"\n')
    assert state.kernel_id == "k1"
    assert jupyter.interrupt("r8") is True
    assert ("POST", "/api/kernels/k1/interrupt", None) in server.calls
    assert jupyter.restart("r8", notebook="a/n.ipynb") is True
    assert ("POST", "/api/kernels/k-nb/restart", None) in server.calls
    assert ran == [("k-nb", "import math")]  # startup runs again after a restart
    assert jupyter.restart("r8", notebook="other.ipynb") is False  # no session yet


def _supervised(home: Path, body: str) -> jupyter.Supervisor:
    (home / "config.toml").write_text(body)
    logged: list[str] = []
    sup = jupyter.Supervisor(logged.append)
    sup.logged = logged  # type: ignore[attr-defined]
    return sup


def test_the_supervisor_starts_an_autostart_runner_once_and_not_after_a_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sup = _supervised(
        home, f'[jupyter.runners.a]\ncwd = "{tmp_path}"\nautostart = true\n[jupyter.runners.b]\n'
    )
    started: list[str] = []

    def fake_start(spec: jupyter.RunnerSpec, timeout: float = 0) -> jupyter.RunnerState:
        started.append(spec.name)
        return jupyter.RunnerState(spec.name, os.getpid(), 1, str(spec.cwd), 0.0)

    monkeypatch.setattr(jupyter, "start", fake_start)
    sup.tick(now=1000.0)
    assert started == ["a"]  # b has no autostart
    jupyter.runner_dir("a").mkdir(parents=True, exist_ok=True)
    jupyter.stop("a")  # the user stops it: the marker stays until a start
    sup.tick(now=1020.0)
    assert started == ["a"]
    (jupyter.runner_dir("a") / "stopped").unlink()
    sup.tick(now=1040.0)
    assert started == ["a", "a"]


def test_the_supervisor_gives_up_on_a_runner_that_keeps_ending(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sup = _supervised(home, f'[jupyter.runners.a]\ncwd = "{tmp_path}"\nautostart = true\n')
    started: list[float] = []
    monkeypatch.setattr(
        jupyter,
        "start",
        lambda spec, timeout=0: (
            started.append(1.0) or jupyter.RunnerState(spec.name, 1, 1, "", 0.0)
        ),
    )
    for i in range(8):
        sup.tick(now=1000.0 + i)
    assert len(started) == jupyter.RESTARTS_PER_HOUR
    assert sum("not starting" in line for line in sup.logged) == 1  # type: ignore[attr-defined]
    sup.tick(now=1000.0 + 4000)  # an hour later it tries again
    assert len(started) == jupyter.RESTARTS_PER_HOUR + 1


def test_the_supervisor_pauses_a_runner_over_its_memory_limit(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sup = _supervised(home, f'[jupyter.runners.m]\ncwd = "{tmp_path}"\nmemory_max = "1G"\n')
    jupyter.runner_dir("m").mkdir(parents=True)
    (jupyter.runner_dir("m") / "token").write_text("t")
    jupyter._write_state(jupyter.RunnerState("m", os.getpid(), 1, str(tmp_path), 0.0))
    monkeypatch.setattr(jupyter, "_http", lambda *a, **k: {})
    paused: list[str] = []
    monkeypatch.setattr(jupyter, "pause", lambda name: paused.append(name) or True)
    monkeypatch.setattr(jupyter, "_rss_mb", lambda pid: 900.0)
    sup.tick(now=1.0)
    assert paused == []
    monkeypatch.setattr(jupyter, "_rss_mb", lambda pid: 1500.0)
    sup.tick(now=2.0)
    assert paused == ["m"] and "over memory_max 1024 MB" in sup.logged[-1]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("value", "mb"),
    [("4G", 4096), ("512M", 512), ("1.5G", 1536), (2048, 2048), ("x", 0), (None, 0)],
)
def test_memory_max_reads_sizes(value: object, mb: int) -> None:
    assert jupyter._megabytes(value) == mb


def test_an_ended_server_is_collected_and_not_taken_for_alive(home: Path) -> None:
    jupyter.runner_dir("z").mkdir(parents=True)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    jupyter._children["z"] = proc
    jupyter._write_state(jupyter.RunnerState("z", proc.pid, 1, "/var/empty", 0.0))
    for _ in range(100):
        if Path(f"/proc/{proc.pid}/stat").exists() and jupyter._alive(proc.pid) is False:
            break
        time.sleep(0.05)
    assert jupyter._alive(proc.pid) is False  # a zombie until its parent waits
    assert jupyter.running("z") is None and "z" not in jupyter._children


def test_an_env_file_is_read_without_expanding_anything(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# comment\n\nexport A=\"two words\"\nB='q'\nC = $HOME/x\nD=\nE=a=b\n")
    assert jupyter.read_env_file(path) == {
        "A": "two words", "B": "q", "C": "$HOME/x", "D": "", "E": "a=b",
    }  # fmt: skip


def test_a_bad_env_file_names_the_line_and_never_shows_it(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("OK=1\nsupersecretvalue\n")
    with pytest.raises(jupyter.RunnerError, match="line 2 is not KEY=VALUE") as caught:
        jupyter.read_env_file(path)
    assert "supersecret" not in str(caught.value)
    with pytest.raises(jupyter.RunnerError, match="env_file"):
        jupyter.read_env_file(tmp_path / "missing")


def test_env_file_is_relative_to_the_runners_directory(home: Path, tmp_path: Path) -> None:
    (home / "config.toml").write_text('[jupyter.runners.e]\nenv_file = ".env"\n')
    assert jupyter.resolve("e", str(tmp_path)).env_file == tmp_path.resolve() / ".env"
    (home / "config.toml").write_text('[jupyter.runners.e]\nenv_file = "/etc/x.env"\n')
    assert jupyter.resolve("e", str(tmp_path)).env_file == Path("/etc/x.env")
    assert jupyter.resolve("other", str(tmp_path)).env_file is None


def _notebook_model(*cells: tuple[str, str, list[str]]) -> dict:
    return {
        "content": {
            "cells": [
                {"cell_type": kind, "source": source, "metadata": {"tags": tags}}
                for kind, source, tags in cells
            ]
        }
    }


def test_replay_runs_the_tagged_code_cells_in_order_and_stops_at_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = _notebook_model(
        ("code", "import a", ["replay"]),
        ("code", "slow()", []),
        ("markdown", "# no", ["replay"]),
        ("code", ["x = ", "1"], ["other", "replay"]),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(jupyter, "_http", lambda *a, **k: model)
    ran: list[str] = []
    outcome = {"status": "ok"}

    def execute(_state, _kernel, code, timeout):
        ran.append(code)
        return jupyter.RunResult(outcome["status"], "", error="NameError: boom\ntrace")

    monkeypatch.setattr(jupyter, "_execute", execute)
    state = jupyter.RunnerState("r", 1, 1, str(tmp_path), 0.0)
    assert jupyter._replay(state, "k", "n.ipynb") == 2
    assert ran == ["import a", "x = 1"]
    ran.clear()
    outcome["status"] = "error"
    with pytest.raises(jupyter.RunnerError, match=r"replay cell 0 of n\.ipynb failed: NameError"):
        jupyter._replay(state, "k", "n.ipynb")
    assert ran == ["import a"]
