"""`cabildo doctor` — one command that answers "is this install actually wired,
and will it work on *this* machine?".

The install spans four places that fail independently and silently:

  * the cabildo hooks in ~/.claude/settings.json — without them a session has the
    MCP tools but never joins the bus, is never listed, and its claims are never
    enforced. Half a wiring is worse than none, because it looks like it worked.
  * the two MCP servers — the `cabildo` bus (registered at user scope, so it is
    there in every folder) and the per-repo `issues` register (project scope, in
    the repo's .mcp.json, and only where a docs/issues/ exists to back it).
  * the SQLite bus itself — reachable, and in WAL. WAL is the one PRAGMA that
    lets a dozen sessions' hook subprocesses read and write without tripping over
    each other; a bus that silently fell back off WAL (a network filesystem, say)
    surfaces a week later as random "database is locked".
  * the daemon — optional, but if one is meant to be draining the intents queue
    and popping notifications, "is it actually up right now" is worth one line.

None of those failures announce themselves near their cause: a missing hook
looks like an agent that just never shows up. And one whole class of failure has
no error at all — cabildo leans on Linux /proc for process identity (the PID
rendezvous that binds an MCP subprocess to the session the hooks see) and on
notify-send for desktop alerts. On macOS both are simply absent, so the install
"succeeds" and then quietly does half of nothing. doctor names that here, at
check time, instead of leaving it to be discovered.

Read-only and best-effort: every probe degrades to a reported failure rather
than a traceback, because a health check that can crash is worse than none.
Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import desktop, mcp_config, procs, service, store

# Verdicts. `na` is "not applicable / nothing to fix here" — an issues server
# with no docs/issues/ to back it, or a daemon that has simply never run. It is
# reported, not counted against the exit code; only `fail` does that.
OK = "ok"
WARN = "warn"
FAIL = "fail"
NA = "na"


@dataclass(frozen=True)
class Check:
    """One line of the report: what was probed, how it came out, and — when it
    came out badly — the single thing to do about it.
    """

    name: str  # dotted key, e.g. "hooks", "mcp.bus", "db", "platform.proc"
    status: str  # OK | WARN | FAIL | NA
    detail: str  # what was found, one line
    fix: str = ""  # remediation for a WARN/FAIL; empty for OK/NA


# Claude Code event name -> the `cabildo-hook <arg>` it must dispatch to. Keyed
# by event only so a missing one reads as its Claude-Code name; the args are the
# authoritative set the dispatcher accepts (cabildo.hooks.cli.EVENTS), guarded
# against drift by a test.
HOOK_EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt_submit",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "Stop": "stop",
    "StopFailure": "stop_failure",
    "SubagentStop": "subagent_stop",
    "SessionEnd": "session_end",
}

# The daemon touches its session every poll (default 2s). Anything seen inside
# this window is "running now"; a stale row is a daemon that died without
# clearing its session. Generous enough to survive a slow `push` holding the loop.
DAEMON_LIVE_WINDOW = 120.0

_HOOK_CMD = re.compile(r"cabildo-hook\s+([a-z_]+)")


def settings_path() -> Path:
    """Claude Code's user settings — where the hooks are wired. expanduser so a
    test (or an unusual $HOME) redirects it without a monkeypatch on this module.
    """
    return Path(os.path.expanduser("~/.claude/settings.json"))


def user_mcp_path() -> Path:
    """Where `claude mcp add --scope user` records user-scope MCP servers."""
    return Path(os.path.expanduser("~/.claude.json"))


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #
def _wired_hook_args(settings: dict) -> set[str]:
    """Every `cabildo-hook <arg>` the settings file dispatches to, regardless of
    which event it hangs under — we match on the command, exactly as the
    installer does, so a full-path command survives a repo move.
    """
    found: set[str] = set()
    for groups in (settings.get("hooks") or {}).values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for hook in (group or {}).get("hooks", []):
                m = _HOOK_CMD.search(str((hook or {}).get("command", "")))
                if m:
                    found.add(m.group(1))
    return found


def check_hooks() -> Check:
    p = settings_path()
    try:
        settings = json.loads(p.read_text())
    except FileNotFoundError:
        return Check("hooks", FAIL, f"no {p}", "run: cabildo install")
    except (OSError, ValueError) as e:
        return Check("hooks", FAIL, f"cannot read {p}: {e}", "run: cabildo install")

    wired = _wired_hook_args(settings)
    missing = [ev for ev, arg in HOOK_EVENTS.items() if arg not in wired]
    total = len(HOOK_EVENTS)
    if missing:
        return Check(
            "hooks",
            FAIL,
            f"{total - len(missing)}/{total} cabildo hooks wired — missing: {', '.join(missing)}",
            "run: cabildo install",
        )
    return Check("hooks", OK, f"{total}/{total} cabildo hooks wired in {p}")


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #
def _servers(config_file: Path) -> dict:
    """The mcpServers object from a Claude/MCP config file, or {} if the file is
    absent or unparseable. Never raises: a broken user config must not stop the
    rest of the checks.
    """
    try:
        data = json.loads(config_file.read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def check_mcp_bus(cwd: str | os.PathLike) -> Check:
    """The bus server. Registered at user scope by the installer, so it is
    present in every folder; a project may also carry it in its own .mcp.json.
    Either is enough — report which.
    """
    scopes = []
    if mcp_config.SERVER_KEY in _servers(user_mcp_path()):
        scopes.append("user")
    if mcp_config.SERVER_KEY in _servers(mcp_config.config_path(cwd)):
        scopes.append("project .mcp.json")
    if scopes:
        return Check("mcp.bus", OK, f"'{mcp_config.SERVER_KEY}' registered ({', '.join(scopes)})")
    return Check(
        "mcp.bus",
        FAIL,
        f"'{mcp_config.SERVER_KEY}' MCP server not registered at user or project scope",
        "run: cabildo install  (user scope), or `cabildo add .` (this folder)",
    )


def check_mcp_issues(cwd: str | os.PathLike) -> Check:
    """The repo's issue register. Its tools are served by the one `cabildo` server
    now, not a separate entry, so this only reports whether a register exists here
    — a tool that answers "no register here" in every session is one the model
    learns to ignore, so absence elsewhere is expected, not a fault.
    """
    if not mcp_config.has_issues(cwd):
        return Check(
            "mcp.issues", NA, "no docs/issues/ here (expected; the agenda tools still work)"
        )
    registered = mcp_config.SERVER_KEY in _servers(mcp_config.config_path(cwd)) or (
        mcp_config.SERVER_KEY in _servers(user_mcp_path())
    )
    if registered:
        return Check("mcp.issues", OK, "docs/issues/ present — served by the cabildo server")
    return Check(
        "mcp.issues",
        FAIL,
        "docs/issues/ exists but the cabildo server is not registered here",
        "run: `cabildo add .` to wire it",
    )


# --------------------------------------------------------------------------- #
# The bus database
# --------------------------------------------------------------------------- #
def check_db() -> Check:
    """Reachable, and in WAL. connect() forces WAL on open, so a mode that comes
    back as anything else means SQLite refused it — the tell for a bus sitting on
    a filesystem (NFS, some overlay/9p mounts) where WAL's shared memory can't
    live, which is exactly where concurrent hook writes will start deadlocking.
    """
    p = store.db_path()
    try:
        conn = store.connect()
    except Exception as e:  # unwritable dir, corrupt file, disk full
        return Check("db", FAIL, f"cannot open {p}: {e}", "check CABILDO_HOME and disk permissions")
    try:
        mode = (conn.execute("PRAGMA journal_mode").fetchone() or ["?"])[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    if str(mode).lower() != "wal":
        return Check(
            "db",
            WARN,
            f"{p} reachable but journal_mode={mode}, not WAL",
            "WAL is refused on some network filesystems — put ~/.cabildo on local disk",
        )
    return Check("db", OK, f"{p} reachable · WAL · {messages} message(s)")


# --------------------------------------------------------------------------- #
# The daemon (optional)
# --------------------------------------------------------------------------- #
def check_daemon(conn=None) -> Check:
    from .daemon import DAEMON_SESSION

    own = conn is None
    if own:
        try:
            conn = store.connect()
        except Exception:
            return Check("daemon", NA, "bus unreachable — cannot tell whether a daemon is up")
    try:
        row = store.get_session(conn, DAEMON_SESSION)
    finally:
        if own:
            conn.close()

    if row is None:
        return Check(
            "daemon",
            NA,
            "no daemon has run on this bus (optional — only push/spawn/notify need one)",
        )
    ago = time.time() - row["last_seen_at"]
    if row["status"] == "active" and ago <= DAEMON_LIVE_WINDOW:
        return Check("daemon", OK, f"live · last beat {_ago(ago)} ago")
    return Check(
        "daemon",
        WARN,
        f"last seen {_ago(ago)} ago — not running now",
        "start it: `cabildo service start` (or check `cabildo service status`)",
    )


def check_daemon_push() -> Check:
    """Whether the installed daemon service will actually push on a push, or is a
    dry run. A fresh `cabildo service install` without --allow-push verifies pushes
    but pushes nothing, which reads exactly like it is pushing when it isn't
    (issue #15) — so name the posture rather than let it look silent.
    """
    p = service.daemon_unit().path
    if not p.exists():
        return Check(
            "daemon.push",
            NA,
            "no daemon service installed (run manually, or `cabildo service install`)",
        )
    try:
        text = p.read_text()
    except OSError as e:
        return Check("daemon.push", WARN, f"cannot read {p}: {e}", "")
    if "--allow-push" in text:
        return Check("daemon.push", OK, "daemon service pushes pushed work (--allow-push)")
    return Check(
        "daemon.push",
        WARN,
        "daemon service is DRY-RUN — `request push` verifies but does NOT push",
        "reinstall with push enabled: `cabildo service install --allow-push`",
    )


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_daemon_build() -> Check:
    """Whether the running daemon is the build installed here. A daemon keeps the
    code it started with, so after an upgrade it lacks new actions until it is
    restarted (issue #42).
    """
    from . import __version__
    from .daemon import info_path

    try:
        info = json.loads(info_path().read_text())
        pid, ran, exe = int(info["pid"]), str(info["version"]), str(info["exe"])
    except (OSError, ValueError, KeyError, TypeError):
        return Check("daemon.build", NA, "no running daemon has recorded its build")
    if not _alive(pid):
        return Check("daemon.build", NA, f"the recorded daemon (pid {pid}) is not running")
    if ran != __version__:
        return Check(
            "daemon.build",
            WARN,
            f"daemon runs {ran}, installed here is {__version__} (pid {pid})",
            "restart it: `cabildo service restart`",
        )
    if os.path.realpath(exe) != os.path.realpath(sys.executable):
        return Check(
            "daemon.build",
            WARN,
            f"daemon runs from {exe}, this cabildo from {sys.executable}",
            "two installs: remove one, or restart the service from the one you want",
        )
    return Check("daemon.build", OK, f"daemon runs {ran}, same as installed")


def check_mcp_fresh() -> Check:
    """MCP servers started before the last upgrade of this install. A session
    keeps the server it started with, so it lacks new tools and actions until
    that session is restarted (issue #42).
    """
    import cabildo

    if not os.path.isdir("/proc"):
        return Check("mcp.fresh", NA, "needs /proc to list running MCP servers")
    try:
        built = Path(cabildo.__file__).stat().st_mtime
    except OSError:
        return Check("mcp.fresh", NA, "cannot read the installed package")
    old = [
        p
        for p in procs.processes()
        if any(a.endswith("cabildo-mcp") for a in p.cmdline.split(" ")[:3]) and p.started_at < built
    ]
    if not old:
        return Check("mcp.fresh", OK, "no MCP server predates the installed build")
    pids = ", ".join(str(p.pid) for p in old[:6])
    return Check(
        "mcp.fresh",
        WARN,
        f"{len(old)} MCP server(s) started before the last upgrade (pid {pids})",
        "restart those Claude sessions, or run `/mcp` in them",
    )


def check_installs() -> Check:
    """More than one ``cabildo`` on PATH: the MCP, hooks and daemon may each pick
    a different one (issue #42).
    """
    found: dict[str, str] = {}
    for d in os.environ.get("PATH", "").split(os.pathsep):
        exe = shutil.which("cabildo", path=d) if d else None
        if exe:
            found.setdefault(os.path.realpath(exe), exe)
    if len(found) <= 1:
        return Check("installs", OK, "one `cabildo` on PATH")
    return Check(
        "installs",
        WARN,
        "several `cabildo` on PATH: " + ", ".join(found.values()),
        "keep one (`uv pip uninstall cabildo` in the others) so the MCP, hooks and daemon agree",
    )


def check_lan() -> list[Check]:
    """Paired machines and how the last pull from each went."""
    from . import lan, pair

    peers = pair.load_peers()
    if not peers:
        return [Check("lan", NA, "no paired machines (pair with `cabildo pair`)")]
    status = lan.load_status()
    checks: list[Check] = []
    for node, peer in peers.items():
        name = f"lan.{node[:8]}"
        st = status.get(node)
        where = f"{peer.host}:{peer.port}"
        if st is None:
            checks.append(
                Check(name, WARN, f"{where} · not pulled yet", "is the daemon running new code?")
            )
        elif st.error:
            checks.append(
                Check(name, WARN, f"{where} · {_ago(time.time() - st.at)} ago · {st.error}", "")
            )
        else:
            checks.append(
                Check(
                    name,
                    OK,
                    f"{where} · last pull {_ago(time.time() - st.at)} ago: "
                    f"{st.pulled} sent, {st.merged} new · cursor {st.cursor}",
                )
            )
    checks.append(check_share_general(len(peers)))
    return checks


def check_share_general(paired: int) -> Check:
    """Whether ``#general`` posts go to the paired machines (``[chat] share_general``)."""
    from . import config

    if not config.share_general():
        return Check("lan.general", OK, "#general stays on this machine")
    return Check(
        "lan.general",
        WARN,
        f"[chat] share_general = true: every #general post goes to {paired} paired machine(s)",
        "set [chat] share_general = false in ~/.cabildo/config.toml to keep #general local",
    )


# --------------------------------------------------------------------------- #
# Platform support — the checks that would have surfaced the macOS gaps
# --------------------------------------------------------------------------- #
def check_platform() -> list[Check]:
    """Flag the features cabildo relies on that this OS does not provide. These
    are WARN, not FAIL: the rest of the install can be perfect and these still be
    absent (macOS), and the tool degrades rather than crashing — but the user
    should hear it from doctor, not from an agent that mysteriously never binds.
    """
    checks: list[Check] = []

    # /proc: the PID rendezvous (session <-> MCP subprocess) and the memory
    # monitor both read it. Absent on macOS/Windows.
    if os.path.isdir("/proc") and os.path.exists(f"/proc/{os.getpid()}/stat"):
        checks.append(Check("platform.proc", OK, "/proc present — process identity works"))
    else:
        checks.append(
            Check(
                "platform.proc",
                WARN,
                "no /proc — the session↔MCP PID rendezvous and the memory monitor are Linux-only",
                "on macOS, spawned agents rely on CABILDO_TAG instead; `mem` won't work",
            )
        )

    # notify-send + a session bus: how the daemon reaches the desktop.
    if desktop.available():
        checks.append(Check("platform.notify", OK, "notify-send + session bus — alerts will show"))
    else:
        checks.append(
            Check(
                "platform.notify",
                WARN,
                "notify-send unavailable — desktop alerts won't show",
                "install libnotify (Linux); macOS has no notify-send, so the daemon stays silent",
            )
        )

    # systemd --user: what `cabildo service` needs to run the daemon in the
    # background. The reason string already carries the macOS launchd hint.
    reason = service.unavailable_reason()
    if reason is None:
        checks.append(
            Check("platform.systemd", OK, "systemd --user available — `cabildo service` can run")
        )
    else:
        checks.append(Check("platform.systemd", WARN, reason, ""))

    return checks


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
def run(cwd: str | os.PathLike | None = None) -> list[Check]:
    """Every check, in reading order: what makes a session join (hooks, MCP),
    what it talks to (db), what serves it (daemon), and whether this machine can
    do any of it at all (platform).
    """
    cwd = cwd if cwd is not None else os.getcwd()
    checks = [check_hooks(), check_mcp_bus(cwd), check_mcp_issues(cwd), check_db()]
    checks.append(check_daemon())
    checks.append(check_daemon_push())
    checks.append(check_daemon_build())
    checks.append(check_mcp_fresh())
    checks.append(check_installs())
    checks.extend(check_lan())
    checks.extend(check_platform())
    return checks


def failed(checks: list[Check]) -> bool:
    """Whether anything is actually broken. WARN and NA do not count — a Mac
    missing /proc, or a bus with no daemon, is not a failed install.
    """
    return any(c.status == FAIL for c in checks)
