"""``cabildo browser login``: log into sites once, in a visible window, for every agent.

Agents run the browser with ``--isolated``: each session gets a fresh in-memory browser, so no two
contend for a profile lock, and each starts logged out. ``login`` is the one place logins are made.
It opens the persistent profile in a visible Chrome, waits until the window is closed, and writes
the cookies to the storage-state file that every isolated browser starts from.

The user runs this command, so the cookie export happens under the user's own hand.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cabildo import procs
from cabildo.browser import fleet

_LEGACY_PROFILE_NAME = "mcp-chrome-for-testing-3b855a0"
_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_POLL_SECONDS = 1.0
_PRIVATE_MODE = 0o600


@dataclass(frozen=True)
class Saved:
    """What ``login`` wrote."""

    path: Path
    cookies: int
    origins: int


def default_profile() -> Path:
    """The profile that holds the logins.

    The profile of the earlier setup when it exists, so its logins are kept. Otherwise a new
    directory under the cabildo data folder.
    """
    legacy = Path.home() / ".cache" / "ms-playwright-mcp" / _LEGACY_PROFILE_NAME
    if legacy.is_dir():
        return legacy
    return Path.home() / ".local" / "share" / "cabildo" / "browser-profile"


def free_profile(
    profile: Path,
    processes: Sequence[procs.Proc],
    *,
    stop: Callable[[Sequence[fleet.Target]], fleet.Stopped] = fleet.stop,
) -> fleet.Stopped:
    """Stop the browsers that hold ``profile`` and remove the lock files they left.

    The lock files stay when a browser survived the stop: it still holds the profile.
    """
    done = stop(fleet.on_profile(processes, profile))
    if not done.left:
        for name in _LOCK_FILES:
            (profile / name).unlink(missing_ok=True)
    return done


def collect(
    read: Callable[[], dict[str, Any] | None],
    *,
    sleep: Callable[[float], None] = time.sleep,
    poll: float = _POLL_SECONDS,
) -> dict[str, Any] | None:
    """Read the browser state until the browser is gone, and return the last good state.

    ``read`` returns the state, or None once the browser is closed. Reading while it is open means
    a crash or a killed window still leaves the latest login.
    """
    last: dict[str, Any] | None = None
    while (state := read()) is not None:
        last = state
        sleep(poll)
    return last


def write_state(path: Path, state: dict[str, Any]) -> Saved:
    """Write ``state`` to ``path`` with mode 600, from the first byte.

    The file is written aside and moved into place, so a server that starts during the write reads
    the old file or the new one, never half of one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    aside = path.with_name(path.name + ".tmp")
    fd = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), _PRIVATE_MODE)
        json.dump(state, handle)
    aside.replace(path)
    return Saved(
        path=path, cookies=len(state.get("cookies", [])), origins=len(state.get("origins", []))
    )


def run(
    profile: Path,
    state_path: Path,
    *,
    channel: str,
    say: Callable[[str], None],
) -> Saved | None:
    """Open ``profile`` in a visible browser, wait for it to close, save the logins.

    Returns None, and leaves the saved file as it was, when the profile could not be freed or
    no state could be read.
    """
    from playwright.sync_api import Error, sync_playwright

    freed = free_profile(profile, procs.processes())
    if freed.left:
        say(
            f"could not stop process(es) {', '.join(map(str, freed.left))} on the profile: not opening it"
        )
        return None
    if freed.targets:
        say(f"freed the profile: stopped {len(freed.gone)} process(es)")
    say("Opening the profile. Log into what you need, then CLOSE THE WINDOW.")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile), channel=channel, headless=False, no_viewport=True
        )

        def read() -> dict[str, Any] | None:
            try:
                return dict(context.storage_state())
            except Error:
                return None

        state = collect(read)
    if state is None:
        say("the browser closed before any login was read: the saved file is unchanged")
        return None
    return write_state(state_path, state)
