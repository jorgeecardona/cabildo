"""The browser servers and Chrome processes agents leave on this machine, and how to stop them.

``find`` turns a list of processes into a ``Fleet``: every browser MCP server with the Chrome
processes under it, and every Chrome that lost its server. ``pick`` chooses what to stop, and
``stop`` stops it. Nothing here uses ``pkill -f``: that pattern matches the command line of the
shell that runs it, and kills it.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cabildo import procs
from cabildo.browser.entry import LaunchFlags, parse_flags

_SERVER_MARKERS = (
    "@playwright/mcp/cli.js",
    "playwright-mcp",
    "cabildo browser serve",
    "cabildo.browser.server",
)
_SESSION_GONE_COMM = frozenset({"systemd", "init", "launchd", "tini", "catatonit", "dumb-init"})
_AGENT_COMM = frozenset({"claude", "codex"})
_GRACE_SECONDS = 3.0
_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Server:
    """One browser MCP server and everything under it."""

    pid: int
    ppid: int
    started_at: float
    flags: LaunchFlags
    session_alive: bool
    owner: int | None
    pids: tuple[int, ...]
    chrome_count: int
    rss_kb: int


@dataclass(frozen=True)
class Stray:
    """A Chrome whose server and parent are gone, with everything under it."""

    pid: int
    started_at: float
    user_data_dir: str | None
    pids: tuple[int, ...]
    rss_kb: int


@dataclass(frozen=True)
class Fleet:
    """Every browser process of this user, grouped."""

    servers: tuple[Server, ...]
    strays: tuple[Stray, ...]

    @property
    def rss_kb(self) -> int:
        """The memory all of it holds."""
        return sum(s.rss_kb for s in self.servers) + sum(s.rss_kb for s in self.strays)


@dataclass(frozen=True)
class Target:
    """Something to stop: a name for the report and the pids to signal."""

    label: str
    pids: tuple[int, ...]
    rss_kb: int


@dataclass(frozen=True)
class Stopped:
    """What ``stop`` did."""

    targets: tuple[Target, ...]
    gone: tuple[int, ...]
    left: tuple[int, ...]

    @property
    def freed_kb(self) -> int:
        """The memory of the targets that are gone, as measured before the signal."""
        gone = set(self.gone)
        return sum(t.rss_kb for t in self.targets if set(t.pids) <= gone)


def _owned(pid: int) -> bool:
    """True when ``pid`` belongs to the current user. Without ``/proc``, assume yes."""
    try:
        return Path(f"/proc/{pid}").stat().st_uid == os.getuid()
    except OSError:
        return not Path("/proc").is_dir()


def _exe(cmdline: str) -> str:
    """The executable name of a command line."""
    argv = cmdline.split()
    return Path(argv[0]).name if argv else ""


def is_server(p: procs.Proc) -> bool:
    """True when ``p`` runs a browser MCP server.

    A shell that merely mentions the server in its arguments does not count: the executable must
    be node, python, cabildo or the server binary itself.
    """
    exe = _exe(p.cmdline)
    runs_code = exe in {"node", "nodejs", "cabildo", "playwright-mcp"} or exe.startswith("python")
    return runs_code and any(marker in p.cmdline for marker in _SERVER_MARKERS)


def is_chrome(p: procs.Proc) -> bool:
    """True when ``p`` is a Chrome that a Playwright driver started."""
    return "--remote-debugging-pipe" in p.cmdline


def _is_main_chrome(p: procs.Proc) -> bool:
    """True for the browser process itself, not its renderers and helpers."""
    return is_chrome(p) and "--type=" not in p.cmdline


def _user_data_dir(p: procs.Proc) -> str | None:
    """The ``--user-data-dir`` of a Chrome command line."""
    return parse_flags(p.cmdline.split()).user_data_dir


def _descendants(root: int, children: dict[int, list[procs.Proc]]) -> list[procs.Proc]:
    """Every process under ``root``, not including it."""
    out: list[procs.Proc] = []
    todo = [root]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child.pid)
    return out


def find(processes: Sequence[procs.Proc], *, owned: Callable[[int], bool] | None = None) -> Fleet:
    """Group the browser processes of this user into servers and strays."""
    is_mine = owned or _owned
    mine = [p for p in processes if is_mine(p.pid)]
    by_pid = {p.pid: p for p in mine}
    children: dict[int, list[procs.Proc]] = {}
    for p in mine:
        children.setdefault(p.ppid, []).append(p)

    servers: list[Server] = []
    covered: set[int] = set()
    for p in mine:
        if not is_server(p):
            continue
        tree = [p, *_descendants(p.pid, children)]
        covered.update(t.pid for t in tree)
        parent = by_pid.get(p.ppid)
        alive = _session_alive(p, parent)
        servers.append(
            Server(
                pid=p.pid,
                ppid=p.ppid,
                started_at=p.started_at,
                flags=parse_flags(p.cmdline.split()),
                session_alive=alive,
                # a client that is not claude or codex: its direct parent is the owner
                owner=_owner_of(p, by_pid) or (p.ppid if alive else None),
                pids=tuple(t.pid for t in tree),
                chrome_count=sum(1 for t in tree if is_chrome(t)),
                rss_kb=sum(t.rss_kb for t in tree),
            )
        )

    strays: list[Stray] = []
    for p in mine:
        if p.pid in covered or not _is_main_chrome(p):
            continue
        parent = by_pid.get(p.ppid)
        if parent is not None and parent.comm not in _SESSION_GONE_COMM and p.ppid > 1:
            continue  # a live process started it: a test run, not a leftover
        tree = [p, *_descendants(p.pid, children)]
        strays.append(
            Stray(
                pid=p.pid,
                started_at=p.started_at,
                user_data_dir=_user_data_dir(p),
                pids=tuple(t.pid for t in tree),
                rss_kb=sum(t.rss_kb for t in tree),
            )
        )
    return Fleet(servers=tuple(servers), strays=tuple(strays))


def _session_alive(server: procs.Proc, parent: procs.Proc | None) -> bool:
    """True when the process that started the server is still a live session.

    A server whose parent is gone, or was adopted by init or systemd, has lost its session.
    """
    return parent is not None and server.ppid > 1 and parent.comm not in _SESSION_GONE_COMM


def on_profile(processes: Sequence[procs.Proc], profile: Path) -> tuple[Target, ...]:
    """The Chrome browsers that hold ``profile``, each with the processes under it.

    A profile directory allows one browser at a time, so these must stop before ``login`` opens it.
    """
    children: dict[int, list[procs.Proc]] = {}
    for p in processes:
        children.setdefault(p.ppid, []).append(p)
    wanted = str(profile)
    out: list[Target] = []
    for p in processes:
        if not _is_main_chrome(p) or _user_data_dir(p) != wanted:
            continue
        tree = [p, *_descendants(p.pid, children)]
        out.append(
            Target(
                f"chrome {p.pid} on the profile",
                tuple(t.pid for t in tree),
                sum(t.rss_kb for t in tree),
            )
        )
    return tuple(out)


def _owner_of(server: procs.Proc, by_pid: dict[int, procs.Proc]) -> int | None:
    """The agent process (``claude`` or ``codex``) above ``server``, or None.

    A wrapper such as ``sh`` or ``npx`` between the agent and the server does not hide it. The
    walk ends with None at init or at a subreaper such as ``systemd --user``: those adopt servers
    whose session died.
    """
    pid = server.ppid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        parent = by_pid.get(pid)
        if parent is None or parent.comm in _SESSION_GONE_COMM:
            return None
        if parent.comm in _AGENT_COMM:
            return pid
        pid = parent.ppid
    return None


def ancestors(pid: int, processes: Sequence[procs.Proc]) -> frozenset[int]:
    """``pid`` and every process above it. These must never be stopped."""
    by_pid = {p.pid: p for p in processes}
    out: set[int] = set()
    while pid > 1 and pid not in out:
        out.add(pid)
        nxt = by_pid.get(pid)
        if nxt is None:
            break
        pid = nxt.ppid
    return frozenset(out)


def pick(
    fleet: Fleet,
    *,
    everything: bool = False,
    include_mine: bool = False,
    protect: frozenset[int] = frozenset(),
) -> tuple[Target, ...]:
    """Choose what to stop.

    By default: the servers whose session is gone, and every stray. With ``everything``: every
    server too, except those started by a process in ``protect`` (the session that runs this
    command) unless ``include_mine`` is set. A tree that holds a protected pid is never chosen.
    """
    out: list[Target] = []
    for s in fleet.servers:
        if protect & set(s.pids):
            continue
        if s.session_alive and not everything:
            continue
        if everything and s.owner in protect and not include_mine:
            continue
        state = "session alive" if s.session_alive else "session gone"
        out.append(Target(f"server {s.pid} ({state})", s.pids, s.rss_kb))
    out.extend(
        Target(f"stray chrome {st.pid}", st.pids, st.rss_kb)
        for st in fleet.strays
        if not protect & set(st.pids)
    )
    return tuple(out)


def stop(
    targets: Sequence[Target],
    *,
    kill: Callable[[int, int], None] = os.kill,
    alive: Callable[[int], bool] = procs._alive,
    sleep: Callable[[float], None] = time.sleep,
    grace: float = _GRACE_SECONDS,
) -> Stopped:
    """Send TERM to every pid of the targets, wait ``grace`` seconds, then send KILL."""
    pids = [pid for t in targets for pid in t.pids]
    _signal_all(pids, signal.SIGTERM, kill)
    waited = 0.0
    while waited < grace and any(alive(pid) for pid in pids):
        sleep(_POLL_SECONDS)
        waited += _POLL_SECONDS
    _signal_all([pid for pid in pids if alive(pid)], signal.SIGKILL, kill)
    sleep(_POLL_SECONDS)
    left = tuple(pid for pid in pids if alive(pid))
    gone = tuple(pid for pid in pids if pid not in left)
    return Stopped(targets=tuple(targets), gone=gone, left=left)


def _signal_all(pids: Sequence[int], sig: int, kill: Callable[[int, int], None]) -> None:
    """Send ``sig`` to each pid. A pid that already exited is not an error."""
    for pid in pids:
        try:
            kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
