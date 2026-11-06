"""Subscription usage limits (Claude Pro / Max) for the logged-in account.

Claude Code keeps the subscription's OAuth token in ``.credentials.json`` in its config
dir; on macOS it keeps the same JSON in the login Keychain, service ``Claude Code-credentials``
(#46). The same endpoint Claude Code's ``/usage`` reads returns how much of the 5-hour and
the 7-day limit is used. It is not a documented API: any failure returns None and the
mode line leaves the row out. An API-key login has no ``claudeAiOauth`` entry, so no row.

Every running workspace on the same Claude config dir (the same account) shares one fetch
and one pace through a file in the runtime directory (``shared``), so they ask the API once
and show the same numbers. Another config dir, another account, has its own file.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.client
import itertools
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from cabildo import store
from cabildo.workspace import sessions, xdgdirs

URL = "https://api.anthropic.com/api/oauth/usage"
BETA = "oauth-2025-04-20"
TIMEOUT = 10.0


@dataclass(frozen=True)
class Limit:
    """One usage limit: percent used, and when it resets (unix seconds, 0 if not known)."""

    percent: float
    resets_at: float


@dataclass(frozen=True)
class PlanUsage:
    """The subscription's name (``max``, ``pro``) and its 5-hour and 7-day limits."""

    plan: str
    five_hour: Limit | None
    seven_day: Limit | None


KEYCHAIN_SERVICE = "Claude Code-credentials"


def _keychain() -> str | None:
    """The credentials JSON from the macOS login Keychain; None off macOS or without it."""
    if sys.platform != "darwin":
        return None
    cmd = ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"]
    if user := os.environ.get("USER"):
        cmd[2:2] = ["-a", user]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _credentials() -> str | None:
    """The credentials JSON: the config dir's file first, then the macOS Keychain."""
    try:
        return (sessions.config_dir() / ".credentials.json").read_text()
    except OSError:
        return _keychain()


def _token() -> tuple[str, str] | None:
    """The subscription name and a not-expired access token; None without one."""
    raw = _credentials()
    if raw is None:
        return None
    try:
        creds = json.loads(raw)
        oauth = creds["claudeAiOauth"]
        token, plan = oauth["accessToken"], oauth.get("subscriptionType") or ""
        expires = float(oauth.get("expiresAt") or 0) / 1000  # milliseconds
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not token or (expires and expires < time.time()):
        return None  # Claude Code refreshes it on its next request
    return plan, token


def _limit(raw: object) -> Limit | None:
    if not isinstance(raw, dict) or raw.get("utilization") is None:
        return None
    try:
        percent = float(raw["utilization"])
    except (ValueError, TypeError):
        return None
    try:  # a reset time it cannot read still shows the percent
        reset = datetime.fromisoformat(raw["resets_at"]).timestamp() if raw.get("resets_at") else 0
    except (ValueError, TypeError):
        reset = 0
    return Limit(percent, reset)


def parse(plan: str, body: dict[str, object]) -> PlanUsage | None:
    """The limits in an ``/api/oauth/usage`` reply; None when it has neither."""
    five, seven = _limit(body.get("five_hour")), _limit(body.get("seven_day"))
    return PlanUsage(plan, five, seven) if five or seven else None


def fetch() -> PlanUsage | None:
    """Ask the API for the account's usage. Blocks up to ``TIMEOUT``: call it in a thread."""
    found = _token()
    if not found:
        return None
    plan, token = found
    req = urllib.request.Request(
        URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": BETA}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError, http.client.HTTPException):  # IncompleteRead is not OSError
        return None
    return parse(plan, body) if isinstance(body, dict) else None


HOUR = 3600.0
WINDOW = {"five_hour": 5 * HOUR, "seven_day": 7 * 24 * HOUR}
# a sample this old weighs half in the pace; the 7-day limit decays slowly, so the last day
# reads almost like a plain average
HALF_LIFE = {"five_hour": 0.5 * HOUR, "seven_day": 24 * HOUR}
KEEP = {key: 4 * half for key, half in HALF_LIFE.items()}  # older samples weigh under 1/16
# the API gives whole percents: no pace until the samples span this long, so one point of
# rounding stays a small part of it (1% in the first 100 s would read 36%/h)
BASELINE = {"five_hour": 0.25 * HOUR, "seven_day": 0.5 * HOUR}
SAME_RESET = 60.0  # the API's reset time jitters by under a second between asks


@dataclass(frozen=True)
class Sample:
    """One limit's percent used, as fetched at ``at`` (unix seconds)."""

    at: float
    percent: float


@dataclass(frozen=True)
class History:
    """One limit's samples in its current window, oldest first, the last ``KEEP`` of them."""

    resets_at: float
    samples: tuple[Sample, ...]


@dataclass(frozen=True)
class PlanHistory:
    """The samples of the 5-hour and the 7-day limit."""

    five_hour: History | None
    seven_day: History | None


def _record(prev: History | None, lim: Limit | None, key: str, now: float) -> History | None:
    """``prev`` with one more sample. A new window (a new reset time, or a lower percent)
    starts a new history.
    """
    if lim is None:
        return None
    sample = Sample(now, lim.percent)
    if (
        prev is None
        or not prev.samples
        or abs(lim.resets_at - prev.resets_at) > SAME_RESET
        or lim.percent < prev.samples[-1].percent
    ):
        return History(lim.resets_at, (sample,))
    kept = tuple(s for s in prev.samples if now - s.at <= KEEP[key])
    return History(lim.resets_at, (*kept, sample))


def record(prev: PlanHistory | None, usage: PlanUsage, now: float) -> PlanHistory:
    """The history after one more ``usage`` sample taken at ``now`` (unix seconds)."""
    return PlanHistory(
        _record(prev.five_hour if prev else None, usage.five_hour, "five_hour", now),
        _record(prev.seven_day if prev else None, usage.seven_day, "seven_day", now),
    )


def rate(samples: tuple[Sample, ...], now: float, half_life: float) -> float | None:
    """Percent per hour: the exponentially weighted average, as of ``now``, of the rate
    between each two samples in a row, each weighted by the integral of
    ``0.5 ** (age / half_life)`` over its time. Uneven gaps weigh by their length, and the
    whole-percent rounding of one step is spread over its interval. None with fewer than
    two distinct times.
    """
    num = den = 0.0
    for a, b in itertools.pairwise(samples):
        if b.at <= a.at:
            continue
        w = 0.5 ** ((now - b.at) / half_life) - 0.5 ** ((now - a.at) / half_life)
        num += w * (b.percent - a.percent) / (b.at - a.at)
        den += w
    return num / den * HOUR if den > 0 else None


@dataclass(frozen=True)
class Pace:
    """How fast one limit fills: percent per hour, as of the sample at ``at``. ``ready`` is
    False until the samples span ``BASELINE``.
    """

    rate: float
    percent: float
    resets_at: float
    at: float
    ready: bool

    def full_at(self) -> float | None:
        """When the limit reaches 100% at this rate; None when the rate is not ready, or
        that is not before the reset.
        """
        if not self.ready or self.rate <= 0:
            return None
        t = self.at + (100 - self.percent) / self.rate * HOUR
        return t if not self.resets_at or t < self.resets_at else None


@dataclass(frozen=True)
class PlanPace:
    """The pace of the 5-hour and the 7-day limit."""

    five_hour: Pace | None
    seven_day: Pace | None


def _pace_of_history(h: History | None, key: str, now: float) -> Pace | None:
    if h is None or not h.samples:
        return None
    last = h.samples[-1]
    r = rate(h.samples, now, HALF_LIFE[key])
    ready = r is not None and last.at - h.samples[0].at >= BASELINE[key]
    return Pace(r or 0.0, last.percent, h.resets_at, last.at, ready)


def pace(history: PlanHistory, now: float) -> PlanPace:
    """The pace of each limit from its samples, weighted as of ``now``."""
    return PlanPace(
        _pace_of_history(history.five_hour, "five_hour", now),
        _pace_of_history(history.seven_day, "seven_day", now),
    )


STALE = HOUR  # an older shared sample is not shown when a new fetch fails
# after a failed fetch (a 429 says "Retry-After: 0", so the wait is ours) nobody fetches for
# this long, doubling on each failure in a row
BACKOFF_FIRST = 120.0
BACKOFF_MAX = 600.0


# The shared file's format. Readers take the keys they know and ignore the rest, so adding a
# key does not change it; bump it only when a key changes meaning or goes away. A workspace
# never writes over a newer format: it shows that file and leaves fetching to the newer one.
FORMAT = 1


@dataclass(frozen=True)
class Stored:
    """The shared file: the last usage, the samples of each limit, and when it was taken."""

    usage: PlanUsage
    history: PlanHistory
    taken_at: float  # time.time() when the usage was fetched
    format: int = FORMAT


@dataclass(frozen=True)
class Shared:
    """What every workspace of this user shows: the usage, its pace, and when it was taken."""

    usage: PlanUsage
    pace: PlanPace
    taken_at: float


def _shown(stored: Stored) -> Shared:
    """The pace is weighted as of the sample, not as of the read, so every workspace that
    reads the same file shows the same numbers.
    """
    return Shared(stored.usage, pace(stored.history, stored.taken_at), stored.taken_at)


def shared(not_before: float) -> Shared | None:
    """The plan usage and pace of any workspace on this Claude config dir, or a new fetch.

    A sample taken before ``not_before`` (epoch seconds) is too old: this process then
    fetches, adds the sample to the shared history, and writes it. A lock file makes the
    other instances wait for that fetch and reuse it, so N instances cost one request per
    window and agree on the pace. A failed fetch is not shared, and nobody fetches again
    until a wait that doubles on each failure in a row (``BACKOFF_FIRST`` to ``BACKOFF_MAX``)
    has passed; meanwhile the last sample is returned while it is under ``STALE`` old, else
    None.
    """
    base = xdgdirs.runtime_home()
    folder = base / xdgdirs.APP
    xdgdirs.make_dirs(folder, base)
    account = hashlib.sha256(os.fspath(sessions.config_dir().resolve()).encode()).hexdigest()[:12]
    # not plan-*.json: sessionmux before 0.1.31 writes a pace there, without samples
    path = folder / f"usage-{account}.json"
    retry = folder / f"plan-{account}.retry"
    with open(folder / f"plan-{account}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes
        cached = _read_stored(path)
        now = time.time()
        fresh = cached is not None and now - cached.taken_at < STALE
        if cached is not None and (cached.taken_at >= not_before or cached.format > FORMAT):
            return _shown(cached) if fresh or cached.format <= FORMAT else None
        until, step = _read_retry(retry)
        usage = fetch() if now >= until else None
        if usage is None:
            if now >= until:  # a fetch was made and failed
                _write_retry(retry, now, step)
            return _shown(cached) if cached and fresh else None
        stored = Stored(usage, record(cached.history if cached else None, usage, now), now)
        _write_stored(path, stored)
        _persist(account, usage, now)
        retry.unlink(missing_ok=True)
        return _shown(stored)


def _persist(account: str, usage: PlanUsage, now: float) -> None:
    """Add each limit's sample to the bus DB. A DB error only loses this sample: the mode line
    does not depend on it.
    """
    try:
        with contextlib.closing(store.connect()) as conn:
            for key, lim in (("five_hour", usage.five_hour), ("seven_day", usage.seven_day)):
                if lim is not None:
                    store.add_plan_sample(conn, account, key, now, lim.percent, lim.resets_at)
    except sqlite3.Error:
        pass


def _read_retry(path: os.PathLike[str]) -> tuple[float, float]:
    """(no fetch before, the last wait); (0, 0) when the last fetch worked."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return float(raw["until"]), float(raw["step"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0, 0.0


def _write_retry(path: os.PathLike[str], now: float, last_step: float) -> None:
    step = min(max(2 * last_step, BACKOFF_FIRST), BACKOFF_MAX)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"until": now + step, "step": step}, fh)
    except OSError:
        pass


def _read_stored(path: os.PathLike[str]) -> Stored | None:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        u, h = raw["usage"], raw.get("history") or {}
        return Stored(
            usage=PlanUsage(
                str(u.get("plan") or ""),
                _limit_of(u.get("five_hour")),
                _limit_of(u.get("seven_day")),
            ),
            history=PlanHistory(_history_of(h.get("five_hour")), _history_of(h.get("seven_day"))),
            taken_at=float(raw["taken_at"]),
            format=int(raw.get("format", 1)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _limit_of(raw: dict[str, Any] | None) -> Limit | None:
    if raw is None:
        return None
    return Limit(float(raw["percent"]), float(raw.get("resets_at") or 0))


def _history_of(raw: dict[str, Any] | None) -> History | None:
    if raw is None:
        return None
    samples = tuple(Sample(float(s["at"]), float(s["percent"])) for s in raw.get("samples", ()))
    return History(float(raw.get("resets_at") or 0), samples)


def _write_stored(path: os.PathLike[str], stored: Stored) -> None:
    tmp = f"{os.fspath(path)}.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(stored), fh)
        os.replace(tmp, path)  # readers see the old file or the new one, never half
    except OSError:
        pass
