"""about.py: what this cabildo is, which build each of its processes runs, and what is up.

One call answers "which version, where, and what needs a restart". It reads only: it starts no
process and changes nothing.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from importlib import metadata

from . import __version__, bus, host, host_client, store
from . import help as help_pages
from .daemon import info_path


@dataclass(frozen=True)
class Running:
    """One running cabildo process and the build it runs."""

    version: str
    pid: int
    exe: str  # "" when the process does not say


@dataclass(frozen=True)
class Socket:
    """A unix socket one of the services listens on."""

    path: str
    live: bool  # something answers on it


@dataclass(frozen=True)
class About:
    """The facts an agent needs about the cabildo it talks to."""

    version: str  # the build of the process that answers
    installed: str  # the version the package metadata of this environment names
    python: str
    exe: str
    home: str
    db: str
    daemon: Running | None
    broker: Socket
    host: Socket
    host_process: Running | None
    host_sessions: int
    docs: tuple[str, ...]  # topics `help` serves
    restart: tuple[str, ...]  # what runs older than what is installed, and the command to fix it


def _installed() -> str:
    try:
        return metadata.version("cabildo")
    except metadata.PackageNotFoundError:
        return ""


def _daemon() -> Running | None:
    try:
        info = json.loads(info_path().read_text())
        return Running(str(info["version"]), int(info["pid"]), str(info.get("exe", "")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _host_process() -> Running | None:
    reply = host_client.ping()
    if reply is None or "version" not in reply:
        return None  # no host, or one built before it reported its version
    return Running(str(reply["version"]), int(reply.get("pid") or 0), "")


def gather() -> About:
    """Collect the facts. Every source that is down reads as None or empty, never as an error."""
    installed = _installed()
    daemon, host_process = _daemon(), _host_process()
    sessions = host_client.list_sessions()
    root = help_pages.docs_dir()
    restart: list[str] = []
    if daemon is not None and installed and daemon.version != installed:
        restart.append(
            f"daemon {daemon.version} is not {installed}: systemctl --user restart "
            "cabildo-daemon.service"
        )
    if host_process is not None and installed and host_process.version != installed:
        restart.append(
            f"host {host_process.version} is not {installed}: it ends every hosted session when "
            "stopped, so do it when none matter (cabildo host list)"
        )
    broker = bus.default_socket_path()
    return About(
        version=__version__,
        installed=installed,
        python=sys.version.split()[0],
        exe=sys.executable,
        home=str(store.home()),
        db=str(store.db_path()),
        daemon=daemon,
        broker=Socket(broker, bus._socket_live(broker)),
        host=Socket(host.socket_path(), host_process is not None),
        host_process=host_process,
        host_sessions=len(sessions),
        docs=tuple(t.name for t in help_pages.topics(root)) if root else (),
        restart=tuple(restart),
    )


def as_dict(about: About) -> dict:
    """The JSON form a tool returns."""
    return asdict(about)
