"""``cabildo browser extension``: install the Chrome extension's local side, and say its state.

``install`` writes three things, and running it again updates them:

- the extension's files, to a folder to load in Chrome (``chrome://extensions``);
- a script Chrome starts, ``<cabildo home>/chrome/host``;
- the native messaging manifest that allows this extension, and no other, to start the script,
  in each Chrome and Chromium profile folder of this user.

It does not load the extension: Chrome only lets the person do that.
"""

from __future__ import annotations

import json
import shlex
import shutil
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cabildo import store
from cabildo.browser.chrome_host import HOST_NAME, socket_dir

# Chrome derives it from the "key" of manifest.json.
EXTENSION_ID = "jofkaobiekhlpkdjekmmokackkmfodag"
SOURCE = Path(__file__).parent / "extension"
_LINUX = ("google-chrome", "google-chrome-beta", "google-chrome-unstable", "chromium")
_MACOS = (
    "Google/Chrome",
    "Google/Chrome Beta",
    "Google/Chrome Dev",
    "Google/Chrome Canary",
    "Chromium",
)


@dataclass(frozen=True)
class Installed:
    """What ``install`` wrote."""

    extension: Path
    manifests: tuple[Path, ...]


def profile_dirs(home: Path | None = None, platform: str = sys.platform) -> tuple[Path, ...]:
    """The Chrome and Chromium profile folders of this user that exist."""
    home = home or Path.home()
    if platform == "darwin":
        base, names = home / "Library" / "Application Support", _MACOS
    else:
        base, names = home / ".config", _LINUX
    return tuple(base / name for name in names if (base / name).is_dir())


def host_script(python: str, cabildo_home: Path) -> str:
    """The script Chrome starts. Chrome passes no environment of ours, so it sets the home."""
    return (
        "#!/bin/sh\n"
        f"export CABILDO_HOME={shlex.quote(str(cabildo_home))}\n"
        f'exec {shlex.quote(python)} -m cabildo.cli browser extension host "$@"\n'
    )


def install(profiles: Sequence[Path], python: str = sys.executable) -> Installed:
    """Write the extension, the host script and a manifest into each of ``profiles``."""
    root = store.home() / "chrome"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    extension = root / "extension"
    shutil.copytree(SOURCE, extension, dirs_exist_ok=True)
    script = root / "host"
    script.write_text(host_script(python, store.home()), encoding="utf-8")
    script.chmod(0o700)
    manifest = {
        "name": HOST_NAME,
        "description": "cabildo: lets its Chrome extension talk to local agents",
        "path": str(script),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    written = []
    for profile in profiles:
        path = profile / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return Installed(extension, tuple(written))


def running_hosts(directory: Path | None = None) -> int:
    """How many Chromes have the extension connected."""
    count = 0
    for path in (directory or socket_dir()).glob("host-*.sock"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))  # closed without a hello: the host ignores it
            except OSError:
                continue
        count += 1
    return count


def status() -> list[str]:
    """The lines of ``cabildo browser extension status``."""
    manifests = [
        path
        for profile in profile_dirs()
        if (path := profile / "NativeMessagingHosts" / f"{HOST_NAME}.json").is_file()
    ]
    lines = [f"manifest: {path}" for path in manifests]
    if not manifests:
        lines.append("not installed: run `cabildo browser extension install`")
    hosts = running_hosts()
    lines.append(
        f"connected: {hosts} Chrome"
        if hosts
        else "not connected: load the extension in Chrome, or open its popup to see why"
    )
    return lines
