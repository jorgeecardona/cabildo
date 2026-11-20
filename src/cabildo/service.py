"""Run cabildo's long-lived pieces as a background service — **systemd --user**
on Linux, **launchd** on macOS.

Why user-space rather than a system unit/daemon: everything these processes
touch is the human's — the SQLite bus under ~/.cabildo, his git worktrees, his
`claude` credentials, his desktop session for notifications. A root-owned
system service would see none of that and would need a pile of
`User=`/`Environment=` patching to pretend otherwise. `systemctl --user` (or a
launchd LaunchAgent, its macOS equivalent) already runs as him, in his session.

One unit:

  cabildo-daemon   drains the intents queue (push, spawn) and pops desktop
                   notifications for messages addressed to you

On Linux it's written to ~/.config/systemd/user/ as a plain-text unit file;
on macOS to ~/Library/LaunchAgents/ as a plist agent. Either way: read it,
edit it, delete it. Nothing here hides state anywhere else.

Two environment details make the difference between "works in my terminal" and
"works as a service":

  * PATH — a service's default PATH is minimal, so the daemon would fail to
    find `claude` or `git` exactly when it tries to spawn an agent. We freeze
    the installing shell's PATH into the unit/agent.
  * DBUS_SESSION_BUS_ADDRESS — notify-send without it falls back to autolaunch
    and pops notifications on a bus nobody is watching. %t/bus is the session
    bus of the user this unit runs as. (Linux only — launchd's `notify-send`
    equivalent, `osascript`/`terminal-notifier`, needs no such address.)

Stdlib only, like the rest of the package — plistlib included.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DAEMON = "daemon"

# Stamped into every unit we write, and looked for before we overwrite one.
# The name is obvious enough that a hand-rolled cabildo-daemon.service is a
# thing people already have — silently replacing it with a different ExecStart
# would break a working setup and leave no trace of what it used to run.
MARKER = "# Written by `cabildo service install`."


@dataclass(frozen=True)
class Unit:
    """One background job we know how to write, start, and take away again —
    a systemd unit on Linux, a launchd agent on macOS. `key`/`description`/
    `exec_start`/`env` are the same either way; only the file format and the
    tool that drives it (`systemctl` vs `launchctl`) differ.
    """

    key: str  # "daemon"
    description: str
    exec_start: list[str]
    restart_sec: int = 5
    env: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Launchd's identifier — reverse-DNS by convention. Keyed the same as
        the systemd unit name so 'daemon' means the same job either way.
        """
        return f"com.cabildo.{self.key}"

    @property
    def name(self) -> str:
        if sys.platform == "darwin":
            return f"{self.label}.plist"
        return f"cabildo-{self.key}.service"

    @property
    def path(self) -> Path:
        return unit_dir() / self.name


# --------------------------------------------------------------------------- #
# Where things live / whether a backend is even an option
# --------------------------------------------------------------------------- #
def unit_dir() -> Path:
    """Where unit/agent files live: the XDG-correct systemd --user dir on
    Linux, ~/Library/LaunchAgents on macOS. Honors overrides so tests (and
    unusual setups) don't have to write into the real location.
    """
    if sys.platform == "darwin":
        base = os.environ.get("CABILDO_LAUNCH_AGENTS_DIR") or os.path.expanduser(
            "~/Library/LaunchAgents"
        )
        return Path(base)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def log_dir() -> Path:
    """Where launchd agent logs push. Systemd doesn't need this — journal
    takes StandardOutput=journal for free — but launchd has no built-in log
    store, so StandardOutPath/StandardErrorPath must point somewhere real.
    """
    base = os.environ.get("CABILDO_LAUNCHD_LOG_DIR") or os.path.expanduser("~/Library/Logs/cabildo")
    return Path(base)


def log_path(unit: Unit) -> Path:
    return log_dir() / f"{unit.label}.log"


def unavailable_reason() -> str | None:
    """None when this platform's service manager can be expected to work here,
    else a human-readable reason. Checked before writing anything: a unit file
    nobody can start is worse than an honest error.
    """
    if sys.platform == "darwin":
        if not shutil.which("launchctl"):
            return "no `launchctl` on PATH — this doesn't look like macOS."
        return None
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
    save_messages: bool = False,
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
    if save_messages:
        cmd.append("--save-messages")
    env = _passthrough_env()
    if notify and sys.platform != "darwin":
        # %t is the unit's runtime dir (/run/user/UID) — where logind puts the
        # session bus notify-send needs to reach GNOME. Meaningless outside
        # systemd, so launchd agents skip it entirely rather than carry a
        # dead env var.
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%t/bus"
    return Unit(
        key=DAEMON,
        description="cabildo daemon — drains the intents queue and notifies the human",
        exec_start=cmd,
        env=env,
    )


GATEWAY = "gateway"


def gateway_unit() -> Unit:
    """The local gateway for Claude Code (``cabildo gateway serve``). Its own service, not part
    of the daemon: a daemon reload or restart must not cut the model streams of every session.
    """
    return Unit(
        key=GATEWAY,
        description="cabildo gateway — local ANTHROPIC_BASE_URL that fails over between upstreams",
        exec_start=[_python(), "-m", "cabildo.cli", "gateway", "serve"],
        env=_passthrough_env(),
    )


def _sh_quote(value: str) -> str:
    """Systemd's own quoting: only what contains whitespace or quotes needs it."""
    if value and not any(c in value for c in " \t\"'\\"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render(unit: Unit) -> str:
    """The unit/agent file, as text. Kept readable on purpose — this is the
    thing the user will `cat` at 3am when notifications stopped arriving.
    """
    if sys.platform == "darwin":
        return _render_plist(unit)
    return _render_systemd(unit)


def _render_systemd(unit: Unit) -> str:
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
ExecReload=/bin/kill -HUP $MAINPID
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


def _render_plist(unit: Unit) -> str:
    """The launchd agent, as an XML property list.

    RunAtLoad + KeepAlive is launchd's `Restart=always`: RunAtLoad starts the
    job the moment it's loaded, and KeepAlive relaunches it if it dies.
    ThrottleInterval is its RestartSec — the minimum gap launchd waits before
    relaunching, so a crash-looping job doesn't spin.

    No shell is involved in ProgramArguments — each argument is its own array
    element — so unlike the systemd ExecStart line, a path containing a space
    needs no quoting at all.
    """
    data: dict[str, object] = {
        "Label": unit.label,
        "Comment": f"{MARKER} Safe to edit; re-running the installer overwrites it.",
        "ProgramArguments": list(unit.exec_start),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": unit.restart_sec,
        "WorkingDirectory": os.path.expanduser("~"),
        "StandardOutPath": str(log_path(unit)),
        "StandardErrorPath": str(log_path(unit)),
    }
    if unit.env:
        data["EnvironmentVariables"] = dict(unit.env)
    return plistlib.dumps(data, fmt=plistlib.FMT_XML).decode("utf-8")


# --------------------------------------------------------------------------- #
# Talking to systemd / launchd
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


def launchctl(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """`launchctl ...`. Never raises, for the same reason `systemctl()` doesn't:
    every caller here would rather report a failure than take the CLI down.
    """
    cmd = ["launchctl", *args]
    try:
        return subprocess.run(cmd, capture_output=capture, text=True, timeout=30)
    except Exception as e:  # no launchctl, hung launchd
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def control(action: str, unit: Unit) -> tuple[int, str]:
    """Start / stop / restart one already-installed unit, on either platform.

    systemd has all three as verbs. launchd has none of them the same way: a
    LaunchAgent starts when its plist is loaded and stops when it is unloaded,
    and 'restart' is unload-then-load. `launchctl start/stop <label>` exist but
    only poke an agent that is already loaded, so they cannot start one that was
    unloaded. load/unload is the pair that actually mirrors systemd here.
    """
    if sys.platform == "darwin":
        if action == "stop":
            p = launchctl("unload", str(unit.path))
        else:  # start or restart
            if action == "restart":
                launchctl("unload", str(unit.path))  # best-effort; may not be loaded
            p = launchctl("load", str(unit.path))
    else:
        p = systemctl(action, unit.name)
    return p.returncode, ((p.stderr or "") + (p.stdout or "")).strip()


def reload(unit: Unit) -> tuple[int, str]:
    """Ask the running daemon to reload: it finishes the intents it is running, takes no new one,
    then starts again on the current code in the same process.

    A restart kills a push in the middle of its gate. A reload waits for it. The flags of the unit
    do not change: after ``cabildo service install`` with new flags, use ``restart``.
    """
    if sys.platform == "darwin":
        p = launchctl("kill", "SIGHUP", f"gui/{os.getuid()}/{unit.label}")
    else:
        p = systemctl("kill", "--signal=HUP", "--kill-whom=main", unit.name)
    return p.returncode, ((p.stderr or "") + (p.stdout or "")).strip()


def exec_start_of(path: Path) -> str:
    """The command a unit/agent file already on disk would run — what you need
    to see before deciding whether replacing it is safe.
    """
    if sys.platform == "darwin":
        try:
            with path.open("rb") as f:
                data = plistlib.load(f)
            return " ".join(data.get("ProgramArguments") or [])
        except (OSError, ValueError):
            return ""
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
    `cabildo-daemon.service` you hand-rolled to run something else is a working
    setup, and clobbering it would leave nothing to restore it from.
    """
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        # launchd creates the log FILE on first write but not its directory —
        # unlike the journal, which needs nothing prepared for it.
        log_dir().mkdir(parents=True, exist_ok=True)
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
    """Write, then enable (and start). Returns one (step, rc, output) per
    systemctl/launchctl call so the caller can show exactly what failed.
    """
    if sys.platform == "darwin":
        return _install_launchd(units, start=start, force=force)
    return _install_systemd(units, start=start, force=force)


def _install_systemd(units: list[Unit], *, start: bool, force: bool) -> list[tuple[str, int, str]]:
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


def _install_launchd(units: list[Unit], *, start: bool, force: bool) -> list[tuple[str, int, str]]:
    """Write the plists, then (unless --no-start) load them. RunAtLoad is
    always on, so the plist itself is `enable`-equivalent — launchd picks it
    up at the next login regardless. Whether we call `launchctl load` *now* is
    the "--now": that's the only thing --no-start withholds.

    An already-loaded agent keeps its OLD ProgramArguments until unloaded and
    reloaded — the same problem `enable --now` + `restart` solves on the
    systemd side — so a currently-loaded copy is unloaded first. That unload
    is expected to fail ("not loaded") on a first install; it isn't surfaced
    as a step for that reason, mirroring how systemd's `restart` never fails
    on a unit that merely wasn't running yet.
    """
    write_units(units, force=force)
    steps: list[tuple[str, int, str]] = []
    for u in units:
        if not start:
            steps.append((f"write {u.name}", 0, "queued for next login — not started (--no-start)"))
            continue
        launchctl("unload", str(u.path))  # best-effort; see docstring
        p = launchctl("load", str(u.path))
        steps.append(
            (f"load {u.name}", p.returncode, ((p.stderr or "") + (p.stdout or "")).strip())
        )
    return steps


def uninstall(units: list[Unit], *, force: bool = False) -> list[tuple[str, int, str]]:
    """Stop, remove the files. Leaves the bus DB alone — uninstalling a
    service must never look like uninstalling your data — and leaves alone
    any unit/agent file we didn't write.
    """
    if sys.platform == "darwin":
        return _uninstall_launchd(units, force=force)
    return _uninstall_systemd(units, force=force)


def _uninstall_systemd(units: list[Unit], *, force: bool) -> list[tuple[str, int, str]]:
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


def _uninstall_launchd(units: list[Unit], *, force: bool) -> list[tuple[str, int, str]]:
    steps: list[tuple[str, int, str]] = []
    for u in units:
        if not force and foreign(u):
            steps.append((f"skip {u.name}", 0, "not written by cabildo — left running"))
            continue
        p = launchctl("unload", str(u.path))
        steps.append(
            (f"unload {u.name}", p.returncode, ((p.stderr or "") + (p.stdout or "")).strip())
        )
        if u.path.exists():
            u.path.unlink()
            steps.append((f"rm {u.path}", 0, ""))
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
    """What the platform's service manager thinks of one unit/agent, plus
    whether we wrote its file at all.
    """
    if sys.platform == "darwin":
        return _status_launchd(unit)
    return _status_systemd(unit)


def _status_systemd(unit: Unit) -> dict:
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


# `launchctl list <label>` prints a quasi-plist dump when the agent is loaded,
# e.g. `"PID" = 1234;` / `"LastExitStatus" = 0;` — not real plist syntax
# (plistlib can't parse it), so it's picked apart line by line instead.
_LAUNCHCTL_LIST_LINE = re.compile(r'^\s*"?(\w+)"?\s*=\s*(.+?);\s*$')


def _status_launchd(unit: Unit) -> dict:
    info = {
        "key": unit.key,
        "name": unit.name,
        "installed": unit.path.exists(),
        "path": str(unit.path),
    }
    p = launchctl("list", unit.label)
    info["loaded"] = p.returncode == 0
    if p.returncode != 0:
        return info
    for line in (p.stdout or "").splitlines():
        m = _LAUNCHCTL_LIST_LINE.match(line)
        if m:
            info[m.group(1)] = m.group(2).strip('"')
    # Map launchd's fields onto the systemd-style keys the status renderer reads,
    # so `service status` is as legible on macOS as on Linux instead of "?/? ?".
    # A loaded agent with a live PID is active/running; a non-zero last exit with
    # no PID is a failure; RunAtLoad makes "loaded" mean "enabled".
    pid = info.get("PID")
    exit_status = info.get("LastExitStatus")
    if pid and pid not in ("0", "-"):
        info["ActiveState"], info["SubState"], info["MainPID"] = "active", "running", pid
    elif exit_status not in (None, "0"):
        info["ActiveState"], info["SubState"] = "failed", "dead"
    else:
        info["ActiveState"], info["SubState"] = "inactive", "dead"
    info["UnitFileState"] = "enabled"
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
