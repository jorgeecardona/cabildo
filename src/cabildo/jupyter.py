"""Jupyter runners: a Jupyter Server that a session starts in a directory, and cells run on it.

A runner is a ``jupyter server`` child process, started detached with ``uv run`` so cabildo
gets no Jupyter dependency: ``jupyter-server`` and ``ipykernel`` live in a throwaway uv
environment. It outlives the process that started it, so a daemon reload or a closed session
does not end it. Its files are in ``<bus home>/jupyter/<name>/``:

- ``state.json``: pid, port, directory and kernel id of the running server.
- ``token``: the server's token (mode 600). It is never put in argv, logs or the bus.
- ``open.html``: a local page that redirects to the server with the token (mode 600), so the
  browser opens a runner without the token in a command line.
- ``server.log``: the server's output.

Cells run through the server's own kernel, over its WebSocket channel
(``/api/kernels/<id>/channels``). A browser that opens a notebook on the same kernel sees the
same variables. The machine-wide ``[jupyter]`` config sets defaults and named runners; a
repo's config is never read, because a runner runs code as you.
"""

from __future__ import annotations

import contextlib
import fcntl
import html
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config, store
from .wsclient import WebSocket, WebSocketError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
START_SECONDS = 180.0  # the first start resolves packages with uv
TEXT_LIMIT = 8000
CHANNEL_SECONDS = 90.0  # the channel answers once the kernel is up: a new kernel is slow
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class RunnerError(Exception):
    """A runner could not start, was not found, or answered wrongly."""


@dataclass(frozen=True)
class RunnerSpec:
    """How to start one runner. Everything but ``name`` and ``cwd`` has a default."""

    name: str
    cwd: Path
    project: Path | None = None  # a uv project: its locked environment is the kernel's
    python: str | None = None  # a uv ``--python`` request: "3.12" or a path; None = uv's default
    packages: tuple[str, ...] = ()  # extra packages for the environment (uv ``--with``)
    kernelspec: str = "python3"
    env: tuple[tuple[str, str], ...] = ()  # set in the server, so every kernel has them
    startup: str = ""  # code run once each time the runner makes its kernel
    port: int = 0  # 0: a free port


@dataclass(frozen=True)
class RunnerState:
    """A running server, as ``state.json`` records it."""

    name: str
    pid: int
    port: int
    cwd: str
    started_at: float
    kernel_id: str = ""
    paused: bool = False  # stopped with SIGSTOP: it answers nothing until resumed


@dataclass(frozen=True)
class RunnerRow:
    """One line of ``statuses``: a runner that is configured or has run before."""

    name: str
    running: bool
    paused: bool
    pid: int | None  # the server's process; the root of the tree to measure
    port: int | None
    cwd: str


@dataclass(frozen=True)
class RunResult:
    """What a cell printed and how it ended."""

    status: str  # ok | error | timeout
    text: str
    error: str = ""  # ``ename: evalue`` and the last traceback lines
    images: int = 0  # how many image outputs the cell made (not returned yet)
    truncated: bool = False


# --------------------------------------------------------------------------- #
# Where things live
# --------------------------------------------------------------------------- #
def root() -> Path:
    """The folder that holds one subfolder per runner."""
    return store.home() / "jupyter"


def runner_dir(name: str) -> Path:
    """The folder of runner ``name``.

    Raises:
        RunnerError: ``name`` is not 1 to 32 characters of ``a-z0-9_-``.
    """
    if not NAME_RE.match(name):
        raise RunnerError(f"runner name {name!r}: use 1 to 32 characters of a-z 0-9 _ -")
    return root() / name


def _private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 600 from the first byte."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- #
# Config: [jupyter] and [jupyter.runners.<name>]
# --------------------------------------------------------------------------- #
def _spec_from(
    name: str, table: dict[str, Any], base: dict[str, Any], cwd: str | None
) -> RunnerSpec:
    merged = {**base, **table}
    env = merged.get("env")
    env_pairs = tuple((str(k), str(v)) for k, v in env.items()) if isinstance(env, dict) else ()
    packages = merged.get("packages")
    where = cwd or merged.get("cwd") or os.getcwd()
    port = merged.get("port", 0)
    python = merged.get("python")
    project = merged.get("project")
    return RunnerSpec(
        name=name,
        cwd=Path(str(where)).expanduser().resolve(),
        project=Path(str(project)).expanduser().resolve() if project else None,
        python=str(python) if python else None,
        packages=tuple(str(p) for p in packages) if isinstance(packages, list) else (),
        kernelspec=str(merged.get("kernelspec") or "python3"),
        env=env_pairs,
        startup=str(merged.get("startup") or ""),
        port=port if isinstance(port, int) else 0,
    )


def named_runners() -> list[str]:
    """The runner names the config declares, sorted."""
    runners = config.jupyter_table().get("runners")
    return sorted(runners) if isinstance(runners, dict) else []


def resolve(name: str, cwd: str | None = None) -> RunnerSpec:
    """The spec of runner ``name``: its config table over the ``[jupyter]`` defaults.

    ``cwd`` (what the asking session wants) wins over the config. A name the config does not
    declare is fine: it gets the defaults, so a session can start one on the fly.
    """
    runner_dir(name)  # validates the name
    table = config.jupyter_table()
    runners = table.get("runners")
    mine = runners.get(name) if isinstance(runners, dict) else None
    base = {k: v for k, v in table.items() if k not in ("runners", "enabled")}
    return _spec_from(name, mine if isinstance(mine, dict) else {}, base, cwd)


# --------------------------------------------------------------------------- #
# State and processes
# --------------------------------------------------------------------------- #
def _read_state(name: str) -> RunnerState | None:
    try:
        data = json.loads((runner_dir(name) / "state.json").read_text(encoding="utf-8"))
        return RunnerState(**data)
    except (OSError, ValueError, TypeError):
        return None


def _write_state(state: RunnerState) -> None:
    _private(runner_dir(state.name) / "state.json", json.dumps(asdict(state)))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _token(name: str) -> str:
    return (runner_dir(name) / "token").read_text(encoding="utf-8").strip()


def _http(
    state: RunnerState, method: str, path: str, body: Any = None, timeout: float = 10.0
) -> Any:
    """One request to the runner's server. Returns the decoded JSON ('' for an empty answer).

    Raises:
        RunnerError: the server refused or did not answer.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{state.port}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"token {_token(state.name)}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RunnerError(f"{method} {path}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RunnerError(f"{method} {path}: {exc}") from exc
    return json.loads(raw) if raw else ""


def running(name: str) -> RunnerState | None:
    """The state of runner ``name`` when its server answers; None when it is not running.

    A state file whose process is gone is removed.
    """
    state = _read_state(name)
    if state is None:
        return None
    if _alive(state.pid):
        if state.paused:
            return state  # a stopped server cannot answer the check
        try:
            _http(state, "GET", "/api/status", timeout=3.0)
        except (RunnerError, OSError):
            return None  # starting, or hung: not usable now
        return state
    (runner_dir(name) / "state.json").unlink(missing_ok=True)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def command(spec: RunnerSpec, port: int) -> list[str]:
    """The command that starts the server: ``uv run``, in the project's environment when the
    spec names a project (the packages are added on top), else in a throwaway one.
    """
    cmd = ["uv", "run", *(["--project", str(spec.project)] if spec.project else ["--no-project"])]
    cmd += ["--with", "jupyterlab", "--with", "ipykernel"]
    for package in spec.packages:
        cmd += ["--with", package]
    if spec.python:
        cmd += ["--python", spec.python]
    cmd += [
        "python", "-m", "jupyter_server",
        "--no-browser", "--ip=127.0.0.1", f"--port={port}", "--ServerApp.port_retries=0",
        f"--ServerApp.root_dir={spec.cwd}",
    ]  # fmt: skip
    return cmd


def start(spec: RunnerSpec, timeout: float = START_SECONDS) -> RunnerState:
    """Start the runner (or return it when it already runs) and wait until it answers.

    Raises:
        RunnerError: ``uv`` is missing, the directory does not exist, the runner already runs
            in another directory, or the server did not answer within ``timeout`` seconds.
    """
    mine = running(spec.name)
    if mine is not None:
        if Path(mine.cwd) != spec.cwd:
            raise RunnerError(f"runner {spec.name!r} runs in {mine.cwd}: stop it first")
        return mine
    if shutil.which("uv") is None:
        raise RunnerError("uv is not on PATH: cabildo starts runners with `uv run`")
    if not spec.cwd.is_dir():
        raise RunnerError(f"{spec.cwd} is not a directory")
    if spec.project and not (spec.project / "pyproject.toml").is_file():
        raise RunnerError(f"{spec.project} has no pyproject.toml: it is not a uv project")
    folder = runner_dir(spec.name)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    (folder / "runtime").mkdir(exist_ok=True)
    token = secrets.token_urlsafe(32)
    _private(folder / "token", token)
    port = spec.port or _free_port()
    env = {
        **os.environ,
        **dict(spec.env),
        "JUPYTER_TOKEN": token,
        "JUPYTER_RUNTIME_DIR": str(folder / "runtime"),
    }
    with (folder / "server.log").open("ab") as log:
        proc = subprocess.Popen(
            command(spec, port), cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )  # fmt: skip
    state = RunnerState(spec.name, proc.pid, port, str(spec.cwd), time.time())
    _write_state(state)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            (folder / "state.json").unlink(missing_ok=True)
            raise RunnerError(
                f"the server ended with code {proc.returncode}: see {folder}/server.log"
            )
        with contextlib.suppress(RunnerError):
            _http(state, "GET", "/api/status", timeout=2.0)
            return state
        time.sleep(0.5)
    stop(spec.name)
    raise RunnerError(f"the server did not answer in {timeout:g} s: see {folder}/server.log")


def stop(name: str) -> bool:
    """Stop runner ``name`` and everything it started. False when it was not running."""
    state = _read_state(name)
    (runner_dir(name) / "state.json").unlink(missing_ok=True)
    if state is None or not _alive(state.pid):
        return False
    _signal_tree(state.pid, signal.SIGCONT)  # a stopped process holds a SIGTERM until it runs
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(state.pid, signal.SIGTERM)  # the server is its own session leader
    for _ in range(50):
        if not _alive(state.pid):
            return True
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(state.pid, signal.SIGKILL)
    return True


def _descendants(pid: int) -> list[int]:
    """Every process below ``pid``, from ``/proc`` (empty where there is none)."""
    out: list[int] = []
    todo = [pid]
    while todo:
        top = todo.pop()
        for tid in Path(f"/proc/{top}/task").glob("*"):
            with contextlib.suppress(OSError, ValueError):
                kids = [int(k) for k in (tid / "children").read_text().split()]
                out += kids
                todo += kids
    return out


def _signal_tree(pid: int, sig: signal.Signals) -> None:
    """Send ``sig`` to the server, its group and every process below it."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)
    for child in _descendants(pid):  # a kernel that left the group
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(child, sig)


def pause(name: str) -> bool:
    """Stop runner ``name`` and its kernels from using the CPU, keeping their memory.

    Returns False when it was not running or already paused. ``resume`` continues it.
    """
    state = running(name)
    if state is None or state.paused:
        return False
    _signal_tree(state.pid, signal.SIGSTOP)
    _write_state(replace(state, paused=True))
    return True


def resume(name: str) -> bool:
    """Continue a paused runner. False when it was not paused."""
    state = _read_state(name)
    if state is None or not state.paused or not _alive(state.pid):
        return False
    _signal_tree(state.pid, signal.SIGCONT)
    _write_state(replace(state, paused=False))
    return True


def statuses() -> list[RunnerRow]:
    """One row per runner that is configured or has a folder, sorted by name."""
    names = set(named_runners())
    if root().is_dir():
        names |= {p.name for p in root().iterdir() if NAME_RE.match(p.name)}
    rows: list[RunnerRow] = []
    for name in sorted(names):
        state = running(name)
        if state is None:
            rows.append(RunnerRow(name, False, False, None, None, str(resolve(name).cwd)))
        else:
            rows.append(RunnerRow(name, True, state.paused, state.pid, state.port, state.cwd))
    return rows


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #
def open_page(name: str, notebook: str | None = None) -> Path:
    """Write ``open.html``, a page that redirects to the runner with its token (mode 600).

    ``notebook`` is a path inside the runner's directory; the page then opens that file.
    """
    state = running(name)
    if state is None:
        raise RunnerError(f"runner {name!r} is not running")
    where = f"/lab/tree/{quote(notebook.lstrip('/'))}" if notebook else "/lab"
    url = f"http://127.0.0.1:{state.port}{where}?token={_token(name)}"
    page = runner_dir(name) / "open.html"
    _private(
        page,
        f'<!doctype html><meta charset="utf-8"><title>{name}</title>'
        f'<meta http-equiv="refresh" content="0;url={html.escape(url)}">',
    )
    return page


def open_link(name: str, notebook: str | None = None, *, launch: bool = False) -> dict[str, str]:
    """What to give the user so they can see the runner: a link, and optionally the browser.

    ``link`` is a ``file://`` page that redirects with the token, so the token is not in the
    link. ``url`` is the server's address without it. ``launch`` opens ``link`` in the user's
    own browser.
    """
    page = open_page(name, notebook)
    state = running(name)
    if state is None:
        raise RunnerError(f"runner {name!r} is not running")
    if launch:
        webbrowser.open(page.as_uri())
    where = f"/lab/tree/{quote(notebook.lstrip('/'))}" if notebook else "/lab"
    return {"link": page.as_uri(), "url": f"http://127.0.0.1:{state.port}{where}"}


# --------------------------------------------------------------------------- #
# Running cells
# --------------------------------------------------------------------------- #
def clip(text: str, limit: int = TEXT_LIMIT) -> tuple[str, bool]:
    """``text`` cut to ``limit`` characters, keeping the head and the tail."""
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters cut] ...\n{text[-half:]}", True


def _message(code: str, session: str, msg_id: str) -> str:
    return json.dumps(
        {
            "header": {
                "msg_id": msg_id, "msg_type": "execute_request", "username": "cabildo",
                "session": session, "date": datetime.now(UTC).isoformat(), "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code, "silent": False, "store_history": True,
                "user_expressions": {}, "allow_stdin": False, "stop_on_error": True,
            },
            "buffers": [],
            "channel": "shell",
        }
    )  # fmt: skip


def _startup(state: RunnerState, kernel_id: str, spec: RunnerSpec) -> None:
    """Run the spec's ``startup`` code on a kernel that was just made."""
    if not spec.startup:
        return
    result = _execute(state, kernel_id, spec.startup, timeout=60.0)
    if result.status != "ok":
        raise RunnerError(f"startup code failed: {result.error or result.status}")


def _ensure_notebook(state: RunnerState, path: str) -> None:
    """Make an empty notebook at ``path`` (inside the runner's directory) when it is not there."""
    url = f"/api/contents/{quote(path)}"
    try:
        _http(state, "GET", f"{url}?content=0")
    except RunnerError as exc:
        if "404" not in str(exc):
            raise
        folder = ""
        for part in path.split("/")[:-1]:  # the server does not make missing folders
            folder = f"{folder}/{part}" if folder else part
            with contextlib.suppress(RunnerError):
                _http(state, "GET", f"/api/contents/{quote(folder)}?content=0")
                continue
            _http(state, "PUT", f"/api/contents/{quote(folder)}", {"type": "directory"})
        empty = {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        _http(state, "PUT", url, {"type": "notebook", "format": "json", "content": empty})


def notebook_path(notebook: str) -> str:
    """``notebook`` as a path inside the runner's directory.

    Raises:
        RunnerError: it is not a relative ``.ipynb`` path that stays inside the directory.
    """
    parts = Path(notebook.strip().lstrip("/")).parts
    if not parts or ".." in parts or not parts[-1].endswith(".ipynb"):
        raise RunnerError(
            f"{notebook!r}: use a path to an .ipynb file inside the runner's directory"
        )
    return "/".join(parts)


def _notebook_kernel(state: RunnerState, spec: RunnerSpec, notebook: str) -> str:
    """The id of the kernel of ``notebook``'s session: the one JupyterLab shows for it.

    A notebook that has no session gets one (and an empty file when it does not exist), with
    the spec's ``startup`` run once. A notebook the user opens later in JupyterLab joins this
    session, so the agent and the user share the variables.
    """
    path = notebook_path(notebook)
    _ensure_notebook(state, path)
    for session in _http(state, "GET", "/api/sessions"):
        if session.get("path") == path and session.get("kernel", {}).get("id"):
            return str(session["kernel"]["id"])
    body = {
        "path": path, "name": Path(path).name, "type": "notebook",
        "kernel": {"name": spec.kernelspec},
    }  # fmt: skip
    made = _http(state, "POST", "/api/sessions", body)
    kernel_id = str(made["kernel"]["id"])
    _startup(state, kernel_id, spec)
    return kernel_id


def _kernel(state: RunnerState, spec: RunnerSpec) -> RunnerState:
    """The state with a live kernel id; makes the kernel (and runs ``startup``) when needed."""
    if state.kernel_id:
        try:
            _http(state, "GET", f"/api/kernels/{state.kernel_id}")
            return state
        except RunnerError:
            pass
    made = _http(state, "POST", "/api/kernels", {"name": spec.kernelspec})
    state = replace(state, kernel_id=str(made["id"]))
    _write_state(state)
    if spec.startup:
        _startup(state, state.kernel_id, spec)
    return state


def _drain(ws: WebSocket, msg_id: str, seconds: float = 5.0) -> None:
    """Read until the interrupted cell is idle, so the next cell does not get aborted with it."""
    deadline = time.monotonic() + seconds
    with contextlib.suppress(TimeoutError, WebSocketError, ValueError):
        while time.monotonic() < deadline:
            ws.settimeout(max(deadline - time.monotonic(), 0.01))
            msg = json.loads(ws.recv())
            done = msg.get("content", {}).get("execution_state") == "idle"
            if msg.get("parent_header", {}).get("msg_id") == msg_id and done:
                return


def _execute(state: RunnerState, kernel_id: str, code: str, timeout: float) -> RunResult:
    """Send ``code`` on kernel ``kernel_id`` and collect what it prints."""
    msg_id, session = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        ws = WebSocket.connect(
            f"ws://127.0.0.1:{state.port}/api/kernels/{kernel_id}/channels",
            headers={"Authorization": f"token {_token(state.name)}"},
            timeout=CHANNEL_SECONDS,
        )
    except (WebSocketError, TimeoutError) as exc:
        raise RunnerError(f"cannot open the kernel channel: {exc}") from exc
    parts: list[str] = []
    error, images = "", 0
    idle = replied = False
    status = "ok"
    deadline = time.monotonic() + timeout
    try:
        ws.send(_message(code, session, msg_id))
        while not (idle and replied):
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            ws.settimeout(left)
            msg = json.loads(ws.recv())
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            kind, content = msg["header"]["msg_type"], msg.get("content", {})
            if kind == "stream":
                parts.append(content.get("text", ""))
            elif kind in ("execute_result", "display_data"):
                data = content.get("data", {})
                images += sum(1 for k in data if k.startswith("image/"))
                if "text/plain" in data and not any(k.startswith("image/") for k in data):
                    parts.append(str(data["text/plain"]) + "\n")
            elif kind == "error":
                tail = "\n".join(content.get("traceback", [])[-20:])
                error = _ANSI.sub("", f"{content.get('ename')}: {content.get('evalue')}\n{tail}")
                status = "error"
            elif kind == "status" and content.get("execution_state") == "idle":
                idle = True
            elif kind == "execute_reply":
                replied = True
                if content.get("status") == "aborted":
                    status, error = "error", "aborted: the kernel was interrupted"
    except TimeoutError:
        status = "timeout"
        with contextlib.suppress(RunnerError):
            _http(state, "POST", f"/api/kernels/{kernel_id}/interrupt")
        _drain(ws, msg_id)  # a cell sent before the interrupt ends is aborted by the kernel
    except WebSocketError as exc:
        raise RunnerError(f"the kernel channel broke: {exc}") from exc
    finally:
        ws.close()
    text, cut = clip(_ANSI.sub("", "".join(parts)))
    return RunResult(status=status, text=text, error=error, images=images, truncated=cut)


def run(name: str, code: str, timeout: float = 30.0, notebook: str | None = None) -> RunResult:
    """Run ``code`` on runner ``name``'s kernel. One cell at a time per runner.

    With ``notebook`` (a path inside the runner's directory) the kernel is that notebook's
    session, the one JupyterLab opens for it. Without it, the runner's own kernel.

    Raises:
        RunnerError: the runner is not running, or the server answered wrongly.
    """
    state = running(name)
    if state is None:
        raise RunnerError(f"runner {name!r} is not running: start it first")
    if state.paused:
        raise RunnerError(f"runner {name!r} is paused: resume it first")
    spec = resolve(name, state.cwd)
    folder = runner_dir(name)
    with (folder / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a kernel runs one cell at a time: wait for the other
        if notebook:
            return _execute(state, _notebook_kernel(state, spec, notebook), code, timeout)
        state = _kernel(state, spec)
        return _execute(state, state.kernel_id, code, timeout)


def main() -> int:  # pragma: no cover (a manual probe)
    """``python -m cabildo.jupyter NAME CODE``: run one cell on a running runner."""
    result = run(sys.argv[1], sys.argv[2])
    print(result.text, result.error)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
