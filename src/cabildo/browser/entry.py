"""The ``cabildo-browser`` MCP entry in ``~/.claude.json``, its saved login, and the doctor checks.

Three facts about the browser that agents use live outside cabildo's database: how the entry
launches the server (``LaunchFlags``), what the saved login holds (``StorageState``), and whether
the entry still obeys the rules (``check``). This module reads them and judges them. It never
writes any of them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ENTRY_NAME = "cabildo-browser"
DEFAULT_STATE = Path.home() / ".claude" / "mcp-servers" / "playwright" / "storage-state.json"
STALE_DAYS = 30
_PRIVATE_MODE = 0o600
_GROUP_OTHER_BITS = 0o077
_SECONDS_PER_DAY = 86400.0

Level = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class LaunchFlags:
    """The flags of a browser server that matter to cabildo, read from its command line."""

    isolated: bool = False
    headless: bool = False
    user_data_dir: str | None = None
    storage_state: str | None = None
    config_file: str | None = None


@dataclass(frozen=True)
class Entry:
    """The ``cabildo-browser`` entry of ``mcpServers`` in ``~/.claude.json``."""

    command: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]

    @property
    def flags(self) -> LaunchFlags:
        """The launch flags in ``args``."""
        return parse_flags(self.args)


@dataclass(frozen=True)
class StorageState:
    """The saved login file that every isolated browser starts from."""

    path: Path
    exists: bool
    cookies: int = 0
    origins: int = 0
    age_days: float = 0.0
    mode: int = 0

    @property
    def empty(self) -> bool:
        """True when the file holds no cookie and no origin."""
        return self.cookies == 0 and self.origins == 0

    @property
    def stale(self) -> bool:
        """True when the file is older than ``STALE_DAYS`` days."""
        return self.age_days > STALE_DAYS


@dataclass(frozen=True)
class Finding:
    """One doctor result. ``fail`` makes ``cabildo browser doctor`` exit 1."""

    name: str
    level: Level
    detail: str


def parse_flags(args: Sequence[str]) -> LaunchFlags:
    """Read the cabildo-relevant flags from a list of arguments.

    Understands ``--flag value`` and ``--flag=value``. Unknown arguments are ignored.
    """
    values: dict[str, str | None] = {}
    tokens = list(args)
    for i, token in enumerate(tokens):
        if not token.startswith("--"):
            continue
        name, eq, inline = token.partition("=")
        if eq:
            values[name] = inline
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        values[name] = nxt if nxt is not None and not nxt.startswith("--") else None
    return LaunchFlags(
        isolated=_truthy(values, "--isolated"),
        headless=_truthy(values, "--headless"),
        user_data_dir=values.get("--user-data-dir"),
        storage_state=values.get("--storage-state"),
        config_file=values.get("--config"),
    )


def _truthy(values: dict[str, str | None], flag: str) -> bool:
    """True when ``flag`` is present and not explicitly ``false``."""
    if flag not in values:
        return False
    return (values[flag] or "true").lower() != "false"


def claude_json() -> Path:
    """The path of ``~/.claude.json`` for the current ``HOME``."""
    return Path.home() / ".claude.json"


def load_entry(path: Path | None = None) -> Entry | None:
    """The ``cabildo-browser`` entry, or None when the file or the entry is missing.

    Raises:
        ValueError: The file is not valid JSON.
    """
    target = path or claude_json()
    try:
        text = target.read_text()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{target} is not valid JSON: {exc}"
        raise ValueError(msg) from exc
    raw = data.get("mcpServers", {}).get(ENTRY_NAME)
    if not isinstance(raw, dict):
        return None
    return Entry(
        command=str(raw.get("command", "")),
        args=tuple(str(a) for a in raw.get("args", [])),
        env=tuple((str(k), str(v)) for k, v in raw.get("env", {}).items()),
    )


def read_storage_state(path: Path, *, now: float | None = None) -> StorageState:
    """What the saved login file holds. A missing or unreadable file reads as empty."""
    try:
        info = path.stat()
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return StorageState(path=path, exists=path.exists())
    moment = time.time() if now is None else now
    return StorageState(
        path=path,
        exists=True,
        cookies=len(data.get("cookies", [])),
        origins=len(data.get("origins", [])),
        age_days=max(0.0, (moment - info.st_mtime) / _SECONDS_PER_DAY),
        mode=info.st_mode & 0o777,
    )


def config_browser(path: str | None) -> dict[str, object]:
    """The ``browser`` section of a Playwright MCP config file. Empty when unreadable."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    section = data.get("browser", {}) if isinstance(data, dict) else {}
    return section if isinstance(section, dict) else {}


def config_headless(path: str | None) -> bool:
    """True when a Playwright MCP config file turns headless on."""
    launch = config_browser(path).get("launchOptions", {})
    return isinstance(launch, dict) and bool(launch.get("headless"))


def check(entry: Entry | None, state: StorageState | None) -> tuple[Finding, ...]:
    """Judge the entry and the saved login against the rules.

    The rules: ``--isolated`` is set, no ``--user-data-dir`` is set, and nothing turns headless on.
    The browser always runs with a visible window.
    """
    if entry is None:
        return (
            Finding("entry", "fail", f"no {ENTRY_NAME!r} entry in mcpServers of ~/.claude.json"),
        )
    flags = entry.flags
    from_config = config_browser(flags.config_file)
    findings = [Finding("entry", "ok", f"{ENTRY_NAME} runs {entry.command}")]
    findings.append(
        Finding("isolated", "ok", "--isolated is set")
        if flags.isolated or from_config.get("isolated") is True
        else Finding("isolated", "fail", "--isolated is missing: agents share one profile lock"),
    )
    data_dir = flags.user_data_dir or from_config.get("userDataDir")
    findings.append(
        Finding("user-data-dir", "fail", f"user data dir {data_dir} brings the lock back")
        if data_dir
        else Finding("user-data-dir", "ok", "no --user-data-dir"),
    )
    if from_config.get("cdpEndpoint"):
        findings.append(
            Finding(
                "cdp-endpoint", "fail", "the config attaches to an existing browser (cdpEndpoint)"
            )
        )
    findings.append(_headless_finding(entry))
    if state is not None:
        findings.extend(_state_findings(state))
    return tuple(findings)


def _headless_finding(entry: Entry) -> Finding:
    """Fail on any way to turn headless on: the flag, an env var, or the config file."""
    flags = entry.flags
    via: list[str] = []
    if flags.headless:
        via.append("--headless")
    via.extend(
        f"env {key}={value}"
        for key, value in entry.env
        if "HEADLESS" in key.upper() and value.lower() not in {"", "0", "false"}
    )
    if config_headless(flags.config_file):
        via.append(f"headless in {flags.config_file}")
    if via:
        return Finding(
            "headed", "fail", f"headless is on ({', '.join(via)}): the browser must be visible"
        )
    return Finding("headed", "ok", "nothing turns headless on")


def _state_findings(state: StorageState) -> list[Finding]:
    """Findings about the saved login file."""
    if not state.exists:
        return [
            Finding(
                "storage-state", "warn", f"{state.path} does not exist: run cabildo browser login"
            )
        ]
    out: list[Finding] = []
    if state.empty:
        out.append(Finding("storage-state", "warn", "holds no login: run cabildo browser login"))
    elif state.stale:
        out.append(
            Finding("storage-state", "warn", f"{state.age_days:.0f} days old (over {STALE_DAYS})")
        )
    else:
        out.append(
            Finding("storage-state", "ok", f"{state.cookies} cookies, {state.origins} origins")
        )
    if state.mode & _GROUP_OTHER_BITS:
        out.append(
            Finding(
                "storage-state-mode", "warn", f"mode is {state.mode:o}, expected {_PRIVATE_MODE:o}"
            )
        )
    return out
