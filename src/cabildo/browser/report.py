"""Plain-text output of ``cabildo browser status``, ``reap`` and ``doctor``.

Each function returns the lines to print. Printing and colors belong to the caller.
"""

from __future__ import annotations

from collections.abc import Sequence

from cabildo.browser.entry import Finding, StorageState
from cabildo.browser.fleet import Fleet, Stopped, Target

_KB_PER_MB = 1024
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60
_MARK = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}


def age(seconds: float) -> str:
    """A short age such as ``12m`` or ``3h05m``."""
    secs = max(0, int(seconds))
    if secs >= _SECONDS_PER_HOUR:
        return f"{secs // _SECONDS_PER_HOUR}h{secs % _SECONDS_PER_HOUR // _SECONDS_PER_MINUTE:02d}m"
    return f"{secs // _SECONDS_PER_MINUTE}m"


def mb(kb: int) -> str:
    """Kilobytes as a whole number of megabytes."""
    return f"{kb // _KB_PER_MB} MB"


def status(fleet: Fleet, state: StorageState | None, *, now: float) -> list[str]:
    """The lines of ``cabildo browser status``."""
    lines = [
        f"{len(fleet.servers)} server(s), "
        f"{sum(s.chrome_count for s in fleet.servers)} chrome process(es) under them, "
        f"{len(fleet.strays)} stray chrome(s), {mb(fleet.rss_kb)} in total"
    ]
    for s in sorted(fleet.servers, key=lambda s: s.started_at):
        flags = ["isolated" if s.flags.isolated else "SHARED PROFILE"]
        if s.flags.headless:
            flags.append("HEADLESS")
        flags.append("session alive" if s.session_alive else "SESSION GONE")
        lines.append(
            f"  server {s.pid:<7} {age(now - s.started_at):>7}  {mb(s.rss_kb):>8}  "
            f"{s.chrome_count} chrome  {', '.join(flags)}"
        )
    for st in fleet.strays:
        lines.append(
            f"  stray  {st.pid:<7} {age(now - st.started_at):>7}  {mb(st.rss_kb):>8}  "
            f"{st.user_data_dir or 'no profile dir'}"
        )
    if state is not None:
        lines.append(_state_line(state))
    return lines


def _state_line(state: StorageState) -> str:
    """One line about the saved login file."""
    if not state.exists:
        return f"saved login: {state.path} is missing (cabildo browser login)"
    if state.empty:
        return f"saved login: {state.path} holds no login (cabildo browser login)"
    note = f", {state.age_days:.0f} days old" + (" (stale)" if state.stale else "")
    return f"saved login: {state.cookies} cookies, {state.origins} origins{note}"


def reap_plan(targets: Sequence[Target]) -> list[str]:
    """The lines of ``cabildo browser reap --dry-run``."""
    if not targets:
        return ["nothing to stop"]
    return [f"would stop {t.label}: {len(t.pids)} process(es), {mb(t.rss_kb)}" for t in targets]


def reap_result(done: Stopped) -> list[str]:
    """The lines printed after ``cabildo browser reap`` stopped something."""
    if not done.targets:
        return ["nothing to stop"]
    lines = [f"stopped {len(done.gone)} process(es), freed about {mb(done.freed_kb)}"]
    if done.left:
        lines.append(f"still alive after KILL: {', '.join(str(p) for p in done.left)}")
    return lines


def doctor(findings: Sequence[Finding]) -> list[str]:
    """The lines of ``cabildo browser doctor``."""
    return [f"{_MARK[f.level]}  {f.name}: {f.detail}" for f in findings]
