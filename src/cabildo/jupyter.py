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

import base64
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
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from collections.abc import Callable
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
KEEP_RUNS = 200  # executions whose files a runner keeps
_PNG = b"\x89PNG\r\n\x1a\n"
REPLAY_TAG = "replay"  # a cell with this tag runs again on every new kernel of its notebook
REPLAY_SECONDS = 600.0
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
    env_file: Path | None = None  # KEY=VALUE lines set in the server; ``env`` wins over them
    port: int = 0  # 0: a free port
    collaboration: bool = True  # jupyter-collaboration: edits made through the API show live
    autostart: bool = False  # the daemon starts it, and starts it again when it ends
    memory_max_mb: int = 0  # the daemon pauses the runner above this much memory; 0: no limit


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
    autostart: bool = False  # the config says the daemon keeps it running


@dataclass(frozen=True)
class OutFile:
    """A rich output of a cell, saved as a file an agent can read (``Read`` shows an image)."""

    path: str
    mime: str
    width: int = 0  # pixels, for a PNG
    height: int = 0
    kind: str = ""  # "plotly" for an HTML figure


@dataclass(frozen=True)
class RunResult:
    """What a cell printed and how it ended."""

    status: str  # ok | error | timeout
    text: str
    error: str = ""  # ``ename: evalue`` and the last traceback lines
    images: tuple[OutFile, ...] = ()
    html: tuple[OutFile, ...] = ()  # plotly figures; other HTML (dataframes) is left out
    truncated: bool = False
    full_text: str = ""  # a file with the whole text, when ``text`` was cut
    execution_count: int = 0  # the kernel's ``In [n]``; 0 when it did not say
    outputs: tuple[dict[str, Any], ...] = ()  # the cell's outputs as nbformat stores them


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
def read_env_file(path: Path) -> dict[str, str]:
    """The variables of a ``.env`` file: ``KEY=VALUE`` lines.

    Blank lines and ``#`` comments are skipped, ``export `` is allowed, and one pair of quotes
    around a value is removed. Nothing is expanded or run.

    Raises:
        RunnerError: the file cannot be read, or a line has no ``=``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RunnerError(f"env_file {path}: {exc}") from exc
    found: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, equals, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not equals or not key:  # the line is not shown: it may hold a secret
            raise RunnerError(f"env_file {path}: line {number} is not KEY=VALUE")
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[key] = value
    return found


def _megabytes(value: object) -> int:
    """``"4G"``, ``"512M"`` or a number of megabytes as megabytes; 0 for anything else."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(int(value), 0)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([MG]?)B?\s*", str(value or ""), re.IGNORECASE)
    if not match:
        return 0
    size = float(match.group(1)) * (1024 if match.group(2).upper() == "G" else 1)
    return int(size)


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
    env_file = merged.get("env_file")
    base_dir = Path(str(where)).expanduser().resolve()
    return RunnerSpec(
        name=name,
        cwd=Path(str(where)).expanduser().resolve(),
        project=Path(str(project)).expanduser().resolve() if project else None,
        python=str(python) if python else None,
        packages=tuple(str(p) for p in packages) if isinstance(packages, list) else (),
        kernelspec=str(merged.get("kernelspec") or "python3"),
        env=env_pairs,
        startup=str(merged.get("startup") or ""),
        env_file=base_dir / Path(str(env_file)).expanduser() if env_file else None,
        port=port if isinstance(port, int) else 0,
        collaboration=merged.get("collaboration") is not False,
        autostart=merged.get("autostart") is True,
        memory_max_mb=_megabytes(merged.get("memory_max")),
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
    """Whether ``pid`` runs. A zombie (ended, not yet collected by its parent) does not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True  # no /proc (macOS): the signal check is all there is
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


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


_children: dict[str, subprocess.Popen[bytes]] = {}  # servers this process started


def running(name: str) -> RunnerState | None:
    """The state of runner ``name`` when its server answers; None when it is not running.

    A state file whose process is gone is removed.
    """
    state = _read_state(name)
    if state is None:
        return None
    if (child := _children.get(name)) is not None and child.poll() is not None:
        del _children[name]  # collected: it had ended
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
    if spec.collaboration:
        cmd += ["--with", "jupyter-collaboration"]
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
    from_file = read_env_file(spec.env_file) if spec.env_file else {}
    env = {
        **os.environ,
        **from_file,
        **dict(spec.env),
        "JUPYTER_TOKEN": token,
        "JUPYTER_RUNTIME_DIR": str(folder / "runtime"),
    }
    (folder / "stopped").unlink(missing_ok=True)  # a start undoes an earlier stop
    with (folder / "server.log").open("ab") as log:
        proc = subprocess.Popen(
            command(spec, port), cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )  # fmt: skip
    _children[spec.name] = proc  # kept so the server is collected, not left a zombie, when it ends
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
    """Stop runner ``name`` and everything it started. False when it was not running.

    A ``stopped`` marker tells the daemon not to start an ``autostart`` runner again.
    """
    state = _read_state(name)
    folder = runner_dir(name)
    if folder.is_dir():
        _private(folder / "stopped", "")
    (folder / "state.json").unlink(missing_ok=True)
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


def live_roots() -> list[tuple[str, Path]]:
    """``(name, directory)`` of every runner whose server process is alive. Reads the state files
    and checks the process, with no HTTP call, so the guard hook can ask on every tool call.
    """
    found: list[tuple[str, Path]] = []
    if not root().is_dir():
        return found
    for folder in root().iterdir():
        if NAME_RE.match(folder.name) and (state := _read_state(folder.name)) and _alive(state.pid):
            found.append((state.name, Path(state.cwd)))
    return found


def statuses() -> list[RunnerRow]:
    """One row per runner that is configured or has a folder, sorted by name."""
    names = set(named_runners())
    if root().is_dir():
        names |= {p.name for p in root().iterdir() if NAME_RE.match(p.name)}
    rows: list[RunnerRow] = []
    for name in sorted(names):
        state = running(name)
        if state is None:
            spec = resolve(name)
            rows.append(RunnerRow(name, False, False, None, None, str(spec.cwd), spec.autostart))
        else:
            auto = resolve(name, state.cwd).autostart
            rows.append(RunnerRow(name, True, state.paused, state.pid, state.port, state.cwd, auto))
    return rows


# --------------------------------------------------------------------------- #
# Supervision, by the daemon
# --------------------------------------------------------------------------- #
RESTARTS_PER_HOUR = 5  # the daemon gives up on a runner that ends this often


def _rss_mb(pid: int) -> float:
    """Resident memory of ``pid`` and everything below it, in megabytes (0 without /proc)."""
    total = 0
    for p in [pid, *_descendants(pid)]:
        with contextlib.suppress(OSError, ValueError, IndexError):
            for line in Path(f"/proc/{p}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])  # kB
    return total / 1024


class Supervisor:
    """Keeps the configured runners the way the config says. The daemon calls ``tick``.

    - ``autostart = true``: start the runner when it is not running, which also brings it back
      after a reboot (the daemon starts at login). A runner you stopped (``stopped`` marker) stays
      stopped until you start it. One that keeps ending is left alone after
      ``RESTARTS_PER_HOUR`` starts in an hour.
    - ``memory_max``: pause a runner whose server and kernels use more than that.

    A runner that a session started on the fly, with no config table, is not supervised.
    """

    def __init__(self, log: Callable[[str], None] = print) -> None:
        self._log = log
        self._starts: dict[str, list[float]] = {}
        self._gave_up: set[str] = set()

    def tick(self, now: float | None = None) -> None:
        """One pass over the configured runners. Never raises: a bad runner is logged."""
        when = time.time() if now is None else now
        for name in named_runners():
            try:
                self._one(resolve(name), when)
            except (RunnerError, OSError) as exc:
                self._log(f"jupyter {name}: {exc}")

    def _one(self, spec: RunnerSpec, now: float) -> None:
        state = running(spec.name)
        if state is not None:
            self._gave_up.discard(spec.name)
            self._limit(spec, state)
            return
        marker = _read_state(spec.name)
        if marker is not None and _alive(marker.pid):
            return  # starting, or too busy to answer: not for us to restart
        if not spec.autostart or (runner_dir(spec.name) / "stopped").exists():
            return
        recent = [t for t in self._starts.get(spec.name, []) if now - t < 3600]
        if len(recent) >= RESTARTS_PER_HOUR:
            if spec.name not in self._gave_up:
                self._gave_up.add(spec.name)
                self._log(
                    f"jupyter {spec.name}: ended {len(recent)} times in an hour, not starting"
                )
            return
        self._starts[spec.name] = [*recent, now]
        self._log(f"jupyter {spec.name}: starting in {spec.cwd}")
        started = start(spec)
        self._log(f"jupyter {spec.name}: running on port {started.port}")

    def _limit(self, spec: RunnerSpec, state: RunnerState) -> None:
        if not spec.memory_max_mb or state.paused:
            return
        used = _rss_mb(state.pid)
        if used > spec.memory_max_mb:
            pause(spec.name)
            self._log(
                f"jupyter {spec.name}: paused, {used:.0f} MB is over memory_max "
                f"{spec.memory_max_mb} MB; `cabildo jupyter restart {spec.name}` frees it"
            )


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


class _Outputs:
    """The files one cell makes, in ``<runner>/out/<time>-<id>/``. The folder is made on the
    first file, so a cell that prints text leaves nothing behind.
    """

    def __init__(self, name: str, msg_id: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.folder = runner_dir(name) / "out" / f"{stamp}-{msg_id[:6]}"
        self.images: list[OutFile] = []
        self.html: list[OutFile] = []

    def _write(self, suffix: str, data: bytes) -> Path:
        if not self.folder.exists():
            self.folder.mkdir(parents=True)
            _prune(self.folder.parent)
        n = len(self.images) + len(self.html) + 1
        path = self.folder / f"{n:02d}{suffix}"
        path.write_bytes(data)
        return path

    def add(self, data: dict[str, Any]) -> bool:
        """Save the image or plotly figure in a display ``data`` bundle; False when none."""
        for mime, suffix in (("image/png", ".png"), ("image/jpeg", ".jpg")):
            if isinstance(data.get(mime), str):
                raw = base64.b64decode(data[mime])
                w, h = struct.unpack(">II", raw[16:24]) if raw[:8] == _PNG else (0, 0)
                self.images.append(OutFile(str(self._write(suffix, raw)), mime, w, h))
                return True
        if isinstance(data.get("image/svg+xml"), str):
            path = self._write(".svg", data["image/svg+xml"].encode())
            self.images.append(OutFile(str(path), "image/svg+xml"))
            return True
        page = data.get("text/html")
        if isinstance(page, str) and "plotly" in page.lower():
            self.html.append(
                OutFile(str(self._write(".html", page.encode())), "text/html", kind="plotly")
            )
            return True
        return False


def _prune(out: Path, keep: int = KEEP_RUNS) -> None:
    """Remove the oldest execution folders of ``out`` beyond ``keep``."""
    old = sorted(p for p in out.iterdir() if p.is_dir())[:-keep]
    for folder in old:
        shutil.rmtree(folder, ignore_errors=True)


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


def _replay(state: RunnerState, kernel_id: str, path: str) -> int:
    """Run the code cells of the notebook at ``path`` tagged ``replay``, in order, on a fresh
    kernel. Returns how many ran. Their stored outputs are left as they are.

    Raises:
        RunnerError: one of them failed; the ones after it did not run.
    """
    model = _http(state, "GET", f"/api/contents/{quote(path)}?content=1&type=notebook")
    notebook = model.get("content") if isinstance(model, dict) else None
    cells = notebook.get("cells", []) if isinstance(notebook, dict) else []
    ran = 0
    for index, cell in enumerate(cells):
        if cell.get("cell_type") != "code" or REPLAY_TAG not in cell.get("metadata", {}).get(
            "tags", []
        ):
            continue
        source = cell.get("source", "")
        code = "".join(source) if isinstance(source, list) else str(source)
        result = _execute(state, kernel_id, code, timeout=REPLAY_SECONDS)
        if result.status != "ok":
            why = result.error.splitlines()[0] if result.error else result.status
            raise RunnerError(f"replay cell {index} of {path} failed: {why}")
        ran += 1
    return ran


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
    the spec's ``startup`` and then the notebook's ``replay`` cells run once. A notebook the user
    opens later in JupyterLab joins this session, so the agent and the user share the variables.
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
    _replay(state, kernel_id, path)
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
    error, count = "", 0
    outputs = _Outputs(state.name, msg_id)
    nb_outputs: list[dict[str, Any]] = []  # what a notebook cell keeps
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
                last = nb_outputs[-1] if nb_outputs else None
                if last and last["output_type"] == "stream" and last["name"] == content["name"]:
                    last["text"] += content.get("text", "")
                else:
                    nb_outputs.append(
                        {"output_type": "stream", "name": content["name"],
                         "text": content.get("text", "")}
                    )  # fmt: skip
            elif kind in ("execute_result", "display_data"):
                data = content.get("data", {})
                shown: dict[str, Any] = {"output_type": kind, "data": data}
                shown["metadata"] = content.get("metadata", {})
                if kind == "execute_result":
                    shown["execution_count"] = content.get("execution_count")
                nb_outputs.append(shown)
                made = outputs.add(data)
                if "text/plain" in data and not made:
                    parts.append(str(data["text/plain"]) + "\n")
            elif kind == "error":
                nb_outputs.append(
                    {"output_type": "error", "ename": content.get("ename", ""),
                     "evalue": content.get("evalue", ""),
                     "traceback": content.get("traceback", [])}
                )  # fmt: skip
                tail = "\n".join(content.get("traceback", [])[-20:])
                error = _ANSI.sub("", f"{content.get('ename')}: {content.get('evalue')}\n{tail}")
                status = "error"
            elif kind == "status" and content.get("execution_state") == "idle":
                idle = True
            elif kind == "execute_reply":
                replied = True
                count = int(content.get("execution_count") or 0)
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
    whole = _ANSI.sub("", "".join(parts))
    text, cut = clip(whole)
    full = ""
    if cut:
        full = str(outputs._write(".txt", whole.encode()))
    return RunResult(
        status=status, text=text, error=error, images=tuple(outputs.images),
        html=tuple(outputs.html), truncated=cut, full_text=full, execution_count=count,
        outputs=tuple(nb_outputs),
    )  # fmt: skip


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


def _find_kernel(state: RunnerState, notebook: str | None) -> str:
    """The kernel id to interrupt or restart: the notebook's session, else the runner's own.
    Empty when there is none yet (nothing was run).
    """
    if not notebook:
        return state.kernel_id
    path = notebook_path(notebook)
    for session in _http(state, "GET", "/api/sessions"):
        if session.get("path") == path and session.get("kernel", {}).get("id"):
            return str(session["kernel"]["id"])
    return ""


def interrupt(name: str, notebook: str | None = None) -> bool:
    """Interrupt the cell that a runner's kernel (or a notebook's) is running.

    It does not wait for the lock of ``run``: a hung cell holds it. False when there is no kernel.

    Raises:
        RunnerError: the runner is not running.
    """
    state = running(name)
    if state is None:
        raise RunnerError(f"runner {name!r} is not running")
    kernel_id = _find_kernel(state, notebook)
    if not kernel_id:
        return False
    _http(state, "POST", f"/api/kernels/{kernel_id}/interrupt")
    return True


def restart(name: str, notebook: str | None = None) -> bool:
    """Restart a runner's kernel (or a notebook's): its variables are lost. ``startup`` runs
    again, then, for a notebook, its cells tagged ``replay``.

    A cell that is running ends. False when there is no kernel yet.

    Raises:
        RunnerError: the runner is not running, or paused.
    """
    state = running(name)
    if state is None:
        raise RunnerError(f"runner {name!r} is not running")
    if state.paused:
        raise RunnerError(f"runner {name!r} is paused: resume it first")
    kernel_id = _find_kernel(state, notebook)
    if not kernel_id:
        return False
    _http(state, "POST", f"/api/kernels/{kernel_id}/restart", timeout=60.0)
    spec = resolve(name, state.cwd)
    with (runner_dir(name) / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _startup(state, kernel_id, spec)
        if notebook:
            _replay(state, kernel_id, notebook_path(notebook))
    return True


def main() -> int:  # pragma: no cover (a manual probe)
    """``python -m cabildo.jupyter NAME CODE``: run one cell on a running runner."""
    result = run(sys.argv[1], sys.argv[2])
    print(result.text, result.error)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
