"""Run cabildo's long-lived pieces as **systemd --user** services.

Why user-space rather than a system unit: everything these processes touch is
the human's — the SQLite bus under ~/.cabildo, his git worktrees, his `claude`
credentials, his desktop session for notifications. A root-owned system unit
would see none of that and would need a pile of `User=`/`Environment=` patching
to pretend otherwise. `systemctl --user` already runs as him, in his runtime
dir, next to his session bus.

Two units, installed independently:

  cabildo-daemon.service   drains the intents queue (land, spawn) and pops
                           desktop notifications for messages addressed to you
  cabildo-sync.service     mirrors this laptop's bus up to the hosted dashboard

Both are written to ~/.config/systemd/user/ and are plain text: read them, edit
them, delete them. Nothing here hides state anywhere else.

Two environment details make the difference between "works in my terminal" and
"works as a service":

  * PATH — systemd --user starts with a minimal PATH, so the daemon would fail
    to find `claude` or `git` exactly when it tries to spawn an agent. We freeze
    the installing shell's PATH into the unit.
  * DBUS_SESSION_BUS_ADDRESS — notify-send without it falls back to autolaunch
    and pops notifications on a bus nobody is watching. %t/bus is the session
    bus of the user this unit runs as.

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DAEMON = "daemon"
SYNC = "sync"

# Stamped into every unit we write, and looked for before we overwrite one.
# These names are obvious enough that a hand-rolled cabildo-sync.service is a
# thing people already have — silently replacing it with a different ExecStart
# would break a working setup and leave no trace of what it used to run.
MARKER = "# Written by `cabildo service install`."


@dataclass(frozen=True)
class Unit:
    """One systemd unit we know how to write, start, and take away again."""

    key: str  # "daemon" | "sync"
    description: str
    exec_start: list[str]
    restart_sec: int = 5
    env: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"cabildo-{self.key}.service"

    @property
    def path(self) -> Path:
        return unit_dir() / self.name


# --------------------------------------------------------------------------- #
# Where things live / whether systemd is even an option
# --------------------------------------------------------------------------- #
def unit_dir() -> Path:
    """XDG-correct home for user units. Honors XDG_CONFIG_HOME so tests (and
    unusual setups) don't have to write into the real ~/.config.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def unavailable_reason() -> str | None:
    """None when `systemctl --user` can be expected to work here, else a
    human-readable reason. Checked before writing anything: a unit file nobody
    can start is worse than an honest error.
    """
    if sys.platform == "darwin":
        return (
            "macOS has no systemd — run `cabildo service install --print` and "
            "wrap the printed ExecStart in a launchd plist, or just run the "
            "daemon under your own supervisor."
        )
    if not shutil.which("systemctl"):
        return "no `systemctl` on PATH — this machine doesn't run systemd."
    if not (os.environ.get("XDG_RUNTIME_DIR") or Path(f"/run/user/{os.getuid()}").exists()):
        return (
            "no user runtime dir — `systemctl --user` needs a logind session "
            "(you may be in a container or a bare ssh without lingering)."
        )
    return None


# --------------------------------------------------------------------------- #
# The units
# --------------------------------------------------------------------------- #
def _python() -> str:
    """The interpreter cabildo is installed in. Using sys.executable rather than
    a `cabildo` on PATH keeps the unit pointed at THIS install even if PATH
    later changes or a second copy shows up.
    """
    return sys.executable


def _passthrough_env() -> dict[str, str]:
    """Environment the service must inherit from the shell that installed it.

    CABILDO_HOME because a non-default bus location is otherwise invisible to a
    service; PATH because systemd --user's default doesn't include ~/.local/bin
    or a nvm/uv shim dir, which is where `claude` usually lives.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    home = os.environ.get("CABILDO_HOME")
    if home:
        env["CABILDO_HOME"] = home
    return env


def daemon_unit(
    *,
    allow_push: bool = False,
    allow_spawn: bool = False,
    max_agents: int = 3,
    max_depth: int = 2,
    budget: float = 0.0,
    budget_days: float = 7.0,
    max_intent_age: float = 2.0,
    notify: bool = True,
) -> Unit:
    """The intents worker. Dry-run by default, exactly like the CLI daemon: a
    service you forgot you installed must not be able to push or spawn.

    The age ceiling matters more for a service than for a hand-started daemon:
    this one comes up at login, whenever that is, and finds whatever the queue
    accumulated while the machine was off.
    """
    cmd = [_python(), "-m", "cabildo.daemon"]
    if allow_push:
        cmd.append("--allow-push")
    if allow_spawn:
        cmd += ["--allow-spawn", "--max-agents", str(max_agents), "--max-depth", str(max_depth)]
    if budget:
        cmd += ["--budget", f"{budget:g}", "--budget-days", f"{budget_days:g}"]
    cmd += ["--max-intent-age", f"{max_intent_age:g}"]
    if not notify:
        cmd.append("--no-notify")
    env = _passthrough_env()
    if notify:
        # %t is the unit's runtime dir (/run/user/UID) — where logind puts the
        # session bus notify-send needs to reach GNOME.
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%t/bus"
    return Unit(
        key=DAEMON,
        description="cabildo daemon — drains the intents queue and notifies the human",
        exec_start=cmd,
        env=env,
    )


def sync_unit(*, interval: float = 60.0, base: str | None = None) -> Unit:
    """The cloud mirror. The token stays in ~/.cabildo/cloud.json (mode 600) —
    never on the unit's command line, where `systemctl cat` and the journal
    would happily republish it.
    """
    cmd = [_python(), "-m", "cabildo.cli", "sync", "--loop", "--interval", f"{interval:g}"]
    if base:
        cmd += ["--base", base]
    return Unit(
        key=SYNC,
        description="cabildo sync — mirrors this machine's bus to the hosted dashboard",
        exec_start=cmd,
        # A cloud that's down or a laptop that's offline is normal; back off
        # harder than the daemon so a dead network doesn't spin the journal.
        restart_sec=30,
        env=_passthrough_env(),
    )


def _sh_quote(value: str) -> str:
    """Systemd's own quoting: only what contains whitespace or quotes needs it."""
    if value and not any(c in value for c in " \t\"'\\"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render(unit: Unit) -> str:
    """The unit file, as text. Kept readable on purpose — this is the thing the
    user will `cat` at 3am when notifications stopped arriving.
    """
    env_lines = "".join(f'Environment="{k}={v}"\n' for k, v in unit.env.items())
    exec_start = " ".join(_sh_quote(a) for a in unit.exec_start)
    return f"""\
{MARKER} Safe to edit; re-running the installer
# overwrites it, so keep local changes in a drop-in:
#   ~/.config/systemd/user/{unit.name}.d/override.conf
[Unit]
Description={unit.description}
Documentation=https://gitlab.com/jorgeecardona/cabildo
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory=%h
{env_lines}Restart=always
RestartSec={unit.restart_sec}
# One process, no forking, no pidfile — the journal is the log.
StandardOutput=journal
StandardError=journal
SyslogIdentifier={unit.name.removesuffix(".service")}

[Install]
WantedBy=default.target
"""


# --------------------------------------------------------------------------- #
# Talking to systemd
# --------------------------------------------------------------------------- #
def systemctl(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """`systemctl --user ...`. Never raises: every caller here would rather
    report a failure than take the CLI down with a traceback.
    """
    cmd = ["systemctl", "--user", *args]
    try:
        return subprocess.run(cmd, capture_output=capture, text=True, timeout=30)
    except Exception as e:  # no systemctl, no session bus, hung manager
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def exec_start_of(path: Path) -> str:
    """The ExecStart line of a unit file already on disk — what you need to see
    before deciding whether replacing it is safe.
    """
    try:
        for line in path.read_text().splitlines():
            if line.startswith("ExecStart="):
                return line[len("ExecStart=") :].strip()
    except OSError:
        pass
    return ""


def foreign(unit: Unit) -> bool:
    """True when a unit file with this name exists but we didn't write it."""
    try:
        return unit.path.exists() and MARKER not in unit.path.read_text()
    except OSError:
        return True  # unreadable — treat as somebody else's and refuse


def write_units(units: list[Unit], *, force: bool = False) -> list[Path]:
    """Write (or overwrite) the unit files. Returns the paths written.

    Refuses to overwrite a unit file we didn't write unless forced: a
    `cabildo-sync.service` you hand-rolled to run something else is a working
    setup, and clobbering it would leave nothing to restore it from.
    """
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for u in units:
        if not force and foreign(u):
            raise FileExistsError(
                f"{u.path} exists and was not written by cabildo "
                f"(ExecStart={exec_start_of(u.path) or '?'})"
            )
        u.path.write_text(render(u))
        os.chmod(u.path, 0o644)
        written.append(u.path)
    return written


def install(
    units: list[Unit], *, start: bool = True, force: bool = False
) -> list[tuple[str, int, str]]:
    """Write, reload, enable (and start). Returns one (step, rc, output) per
    systemctl call so the caller can show exactly what failed.
    """
    write_units(units, force=force)
    steps: list[tuple[str, int, str]] = []

    def run(label: str, *args: str) -> None:
        p = systemctl(*args)
        steps.append((label, p.returncode, ((p.stderr or "") + (p.stdout or "")).strip()))

    run("daemon-reload", "daemon-reload")
    for u in units:
        if start:
            run(f"enable --now {u.name}", "enable", "--now", u.name)
            # A unit that was already running keeps the OLD ExecStart until it
            # is restarted; `enable --now` alone would silently leave it there.
            run(f"restart {u.name}", "restart", u.name)
        else:
            run(f"enable {u.name}", "enable", u.name)
    return steps


def uninstall(units: list[Unit], *, force: bool = False) -> list[tuple[str, int, str]]:
    """Stop, disable, remove the files, reload. Leaves the bus DB alone —
    uninstalling a service must never look like uninstalling your data — and
    leaves alone any unit file we didn't write.
    """
    steps: list[tuple[str, int, str]] = []
    for u in units:
        if not force and foreign(u):
            steps.append((f"skip {u.name}", 0, "not written by cabildo — left running"))
            continue
        for verb in ("stop", "disable"):
            p = systemctl(verb, u.name)
            steps.append(
                (f"{verb} {u.name}", p.returncode, ((p.stderr or "") + (p.stdout or "")).strip())
            )
        if u.path.exists():
            u.path.unlink()
            steps.append((f"rm {u.path}", 0, ""))
    p = systemctl("daemon-reload")
    steps.append(("daemon-reload", p.returncode, (p.stderr or "").strip()))
    return steps


_SHOW_PROPS = (
    "UnitFileState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainStartTimestamp",
    "NRestarts",
    "Result",
)


def status(unit: Unit) -> dict:
    """What systemd thinks of one unit, plus whether we wrote its file at all."""
    info = {
        "key": unit.key,
        "name": unit.name,
        "installed": unit.path.exists(),
        "path": str(unit.path),
    }
    p = systemctl("show", unit.name, *[f"--property={k}" for k in _SHOW_PROPS])
    for line in (p.stdout or "").splitlines():
        k, _, v = line.partition("=")
        if k in _SHOW_PROPS:
            info[k] = v
    return info


def linger_enabled() -> bool | None:
    """Whether the user's services survive logout. None when we can't tell."""
    try:
        p = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return (p.stdout or "").strip().lower() in ("yes", "true", "1")


def enable_linger() -> tuple[int, str]:
    """Ask logind to keep this user's services running after logout. Needs a
    polkit prompt or root, so it can legitimately fail — report, don't die.
    """
    try:
        p = subprocess.run(
            ["loginctl", "enable-linger", str(os.getuid())],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        return 1, str(e)
    return p.returncode, ((p.stderr or "") + (p.stdout or "")).strip()
