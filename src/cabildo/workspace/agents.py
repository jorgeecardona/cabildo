"""Live session state from Claude Code's own `claude agents --json`.

Read-only. Used to mark rows (running / waiting / working), to open background
sessions with `claude attach`, and to tell which session the main pane shows
after ← in Claude (the pane title is the shown agent's name).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass

from cabildo.workspace import xdgdirs


@dataclass(frozen=True)
class AgentState:
    session_id: str
    agent_id: str  # short id: what `claude attach` takes
    kind: str  # interactive | background
    name: str  # what Claude puts in the terminal title
    cwd: str
    status: str  # busy | waiting | idle            (live interactive)
    state: str  # working | blocked | done | ...    (background)
    pid: int | None  # set while the process is alive

    @property
    def needs_input(self) -> bool:
        return self.status == "waiting" or self.state == "blocked"

    @property
    def working(self) -> bool:
        return self.status == "busy" or self.state == "working"

    @property
    def alive(self) -> bool:
        return self.pid is not None or self.state in ("working", "blocked")

    @property
    def attachable(self) -> bool:
        return self.kind == "background"


def _run(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    # stdin from /dev/null: the app's terminal belongs to the app
    return subprocess.run(
        ["claude", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _text(entry: dict, key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def live_agents() -> dict[str, AgentState] | None:
    """session_id -> AgentState; None when `claude agents` failed (then nothing is known,
    which is not the same as nothing running). Takes ~0.35 s — call it off the UI thread.
    """
    try:
        r = _run("agents", "--json", "--all", timeout=6)
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not isinstance(data, list):
        return None
    out: dict[str, AgentState] = {}
    for e in data:
        if not isinstance(e, dict):
            continue
        sid = _text(e, "sessionId")
        if sid:
            pid = e.get("pid")
            out[sid] = AgentState(
                session_id=sid,
                agent_id=_text(e, "id") or sid[:8],
                kind=_text(e, "kind"),
                name=_text(e, "name"),
                cwd=_text(e, "cwd"),
                status=_text(e, "status"),
                state=_text(e, "state"),
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            )
    return out


@dataclass(frozen=True)
class Snapshot:
    """One `claude agents` result; ``agents`` is None when the call failed."""

    agents: dict[str, AgentState] | None
    taken_at: float  # time.time() when the call started


def shared_live_agents(not_before: float) -> Snapshot:
    """The last `claude agents` snapshot of any workspace of this user, or a new one.

    A snapshot taken before ``not_before`` (epoch seconds) is too old: then this
    process runs `claude agents` and shares the result. A lock file makes the other
    instances wait for that call and reuse it, so N instances cost one call, not N.
    """
    base = xdgdirs.runtime_home()
    folder = base / xdgdirs.APP
    xdgdirs.make_dirs(folder, base)
    path = folder / "agents.json"
    with open(folder / "agents.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes
        cached = _read_shared(path)
        if cached is not None and cached.taken_at >= not_before:
            return cached
        started = time.time()
        snap = Snapshot(agents=live_agents(), taken_at=started)
        if snap.agents is not None:  # a failed call is not shared: the next one retries
            _write_shared(path, snap)
        return snap


def _read_shared(path: os.PathLike[str]) -> Snapshot | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        found = {e["session_id"]: AgentState(**e) for e in data["agents"]}
        return Snapshot(agents=found, taken_at=float(data["taken_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_shared(path: os.PathLike[str], snap: Snapshot) -> None:
    agents = [asdict(a) for a in (snap.agents or {}).values()]
    tmp = f"{os.fspath(path)}.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"taken_at": snap.taken_at, "agents": agents}, fh)
        os.replace(tmp, path)  # readers see the old file or the new one, never half
    except OSError:
        pass


def remove(agent_id: str) -> str | None:
    """Stop and delete a background session (its agent-view row, and a worktree
    Claude made for it when that is safe). None on success, else Claude's reason
    for refusing, e.g. a worktree with unpushed commits.
    """
    try:
        _run("stop", agent_id, timeout=20)
        r = _run("rm", agent_id, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if r.returncode == 0:
        return None
    lines = (r.stderr or r.stdout).strip().splitlines()
    return lines[0] if lines else f"`claude rm {agent_id}` failed"


def wait_exit(pids: list[int], timeout: float = 5.0) -> bool:
    """Wait until these processes are gone (so none writes its transcript again)."""
    end = time.monotonic() + timeout
    while True:
        alive = [p for p in pids if _alive(p)]
        if not alive:
            return True
        if time.monotonic() > end:
            return False
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    """Running, i.e. it exists and is not a zombie (exited, not yet reaped by its parent)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]  # field 3; the name may hold spaces
    except (OSError, IndexError):
        return True
    return state != "Z"


_SPINNER = re.compile(r"^[^\w(\[]+")  # leading status glyphs: ✳ ✻ ⠂ ...


def title_name(title: str) -> str:
    """The name in a terminal title, without Claude's spinner glyph."""
    return _SPINNER.sub("", title).strip()


def session_for_title(title: str, agents: dict[str, AgentState], current: str | None) -> str | None:
    """The session a terminal title names, or None when it names none or several.

    Claude sets the title to the shown session's name (with a spinner glyph
    while working), and to "... · claude agents" in agent view. This is
    observed behaviour on 2.1.302, not a documented interface.
    """
    name = title_name(title)
    if not name:
        return None
    if name.endswith("…"):
        stem = name[:-1].rstrip()
        hits = [a.session_id for a in agents.values() if a.name and a.name.startswith(stem)]
    else:
        hits = [a.session_id for a in agents.values() if a.name == name]
    if current in hits:
        return current
    return hits[0] if len(hits) == 1 else None
