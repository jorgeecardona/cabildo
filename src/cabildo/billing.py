"""What Anthropic says the organization was billed, as opposed to what this machine's
transcripts add up to.

Three modules, three different claims, and keeping them apart is the point:

  * ``cabildo.usage`` -- the tokens this machine's transcripts recorded;
  * ``cabildo.prices`` -- what a published rate card says those tokens cost;
  * here -- what ``GET /v1/organizations/analytics/cost`` reports the organization
    actually spent, after discounts and before credits.

The billed figure is authoritative and the computed one is not, but it does not replace
it, for two reasons. It covers the whole organization -- every machine, every API key,
every workspace -- where the computed figure covers this laptop. And it lags: data
typically lands within 4 hours and can take 24, so today's billed cost is absent or
partial exactly when someone looks. The two are shown side by side, each labelled.

Credentials never live in cabildo's config. ``[billing].key_env`` names an environment
variable to read, and ``[billing].key_command`` a command whose stdout is the key, for a
keychain or a password manager. The key itself is never written to the bus DB, a log, or
a config file. It must be an Admin API key (``sk-ant-admin...``) or an ``org:admin``
token: the plain key Claude Code runs on is rejected by these endpoints.

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import http.client
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import __version__, config

URL = "https://api.anthropic.com/v1/organizations/analytics/cost"
BETA = "analytics-usage-cost-2026-08-24"
VERSION = "2026-10-21"
USER_AGENT = f"cabildo/{__version__} (https://gitlab.com/cardona-oss/cabildo)"
TIMEOUT = 20.0

#: The API's own limits, which the caller must respect rather than discover through
#: errors: one request spans at most 31 days, and the history starts here and reaches
#: back at most a year.
MAX_SPAN = timedelta(days=31)
EARLIEST = datetime(2026, 1, 1, tzinfo=UTC)
MAX_LOOKBACK = timedelta(days=365)

#: Amounts come back as decimal strings in cents ("41280.000000" is $412.80).
CENTS = 100.0

#: A page of rows; 1-day buckets over 31 days cannot exceed this, and asking for more
#: than one page per chunk would mean the bucket width was not applied.
PAGE_LIMIT = 1000


class BillingError(Exception):
    """A query that could not be answered. Carries a sentence fit to show a human."""


@dataclass(frozen=True)
class Day:
    """One UTC day's billed cost, in dollars."""

    day: str  # YYYY-MM-DD, the bucket's UTC start
    dollars: float


@dataclass(frozen=True)
class Billed:
    """Billed days, newest last, and how far the data is complete.

    ``refreshed_at`` is the API's ``data_refreshed_at``: cost after it is not yet
    counted, so a window ending later is a floor. None when the API returned no data at
    all, which means "nothing has landed for this range yet", not "zero was spent".
    """

    days: tuple[Day, ...]
    refreshed_at: datetime | None

    def total(self) -> float:
        return sum(d.dollars for d in self.days)

    def since(self, start: datetime) -> float:
        """Billed dollars from ``start`` (UTC) onward."""
        key = start.astimezone(UTC).strftime("%Y-%m-%d")
        return sum(d.dollars for d in self.days if d.day >= key)


# --------------------------------------------------------------------------- #
# The credential
# --------------------------------------------------------------------------- #
DEFAULT_KEY_ENV = "ANTHROPIC_ADMIN_KEY"


def admin_key(cwd: str | None = None) -> str | None:
    """The Admin API key, from the environment variable or command the config names.

    None when none is configured or the command fails: billing is an extra, and a
    missing key is a normal state, not an error to report.
    """
    name = str(config.get(cwd, "billing", "key_env", default=DEFAULT_KEY_ENV))
    if key := os.environ.get(name, "").strip():
        return key
    command = str(config.get(cwd, "billing", "key_command", default="")).strip()
    if not command:
        return None
    try:
        # Split, not a shell: the command is read from a config file, and a shell would
        # turn every character in it into something that can run. A pipeline that really
        # needs one goes in a script, which this can then call by name.
        out = subprocess.run(
            shlex.split(command), capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


# --------------------------------------------------------------------------- #
# The query
# --------------------------------------------------------------------------- #
def _chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """``start`` to ``end`` cut into spans the API accepts, oldest first."""
    spans, at = [], start
    while at < end:
        stop = min(at + MAX_SPAN, end)
        spans.append((at, stop))
        at = stop
    return spans


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(key: str, params: list[tuple[str, str]]) -> dict:
    """One GET, parsed. Raises BillingError with a sentence a human can act on."""
    url = f"{URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": key,
            "anthropic-version": VERSION,
            "anthropic-beta": BETA,
            "user-agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise BillingError(_http_reason(e)) from e
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise BillingError(f"could not reach the cost API: {e}") from e
    if not isinstance(body, dict):
        raise BillingError("the cost API answered with something that is not an object")
    return body


def _http_reason(e: urllib.error.HTTPError) -> str:
    """Why the call failed, in terms of what to do about it.

    401 and 403 here almost always mean the configured key is an ordinary API key. The
    endpoint rejects those, and its own message does not say so plainly, so this does.
    """
    if e.code in (401, 403):
        return (
            "the cost API rejected the key: it needs an Admin API key (sk-ant-admin…) "
            "or an org:admin token, not the key Claude Code sends"
        )
    if e.code == 404:
        return "the cost API is not available to this organization"
    if e.code == 410:
        return "the page cursor expired while the data refreshed; ask again"
    if e.code == 429:
        return "the cost API is rate limiting; ask again later"
    return f"the cost API answered {e.code}"


def _rows(body: dict) -> list[Day]:
    out = []
    for row in body.get("data") or []:
        if not isinstance(row, dict):
            continue
        cost, start = row.get("cost") or {}, row.get("starting_at") or ""
        try:
            dollars = float(cost["amount"]) / CENTS
        except (KeyError, TypeError, ValueError):
            continue
        if len(start) >= 10:
            out.append(Day(start[:10], dollars))
    return out


def _refreshed(body: dict) -> datetime | None:
    raw = body.get("data_refreshed_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch(key: str, start: datetime, end: datetime) -> Billed:
    """Billed cost per UTC day over ``start``..``end``, summed across the organization.

    No ``group_by``: one row per day is all a total needs, and every dimension left
    ungrouped keeps the row count at one per bucket, well inside a single page.
    """
    floor = max(EARLIEST, datetime.now(UTC) - MAX_LOOKBACK)
    start = max(start.astimezone(UTC), floor)
    end = end.astimezone(UTC)
    if start >= end:
        return Billed((), None)
    per_day: dict[str, float] = {}
    refreshed: datetime | None = None
    for chunk_start, chunk_end in _chunks(start, end):
        page: str | None = None
        while True:
            params = [
                ("starting_at", _stamp(chunk_start)),
                ("ending_at", _stamp(chunk_end)),
                ("time_bucket_width", "1d"),
                ("order_by", "time"),
                ("limit", str(PAGE_LIMIT)),
            ]
            if page:
                params.append(("page", page))
            body = _request(key, params)
            for row in _rows(body):
                per_day[row.day] = per_day.get(row.day, 0.0) + row.dollars
            seen = _refreshed(body)
            if seen and (refreshed is None or seen < refreshed):
                refreshed = seen  # the earliest horizon any chunk reported
            page = body.get("next_page") if body.get("has_more") else None
            if not page:
                break
    days = tuple(Day(day, per_day[day]) for day in sorted(per_day))
    return Billed(days, refreshed)


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #
#: The API's data lands in 4 hours and sometimes 24, so asking more often than this
#: spends requests to re-read the same numbers.
REFRESH = 1800.0
BACKOFF_FIRST = 300.0
BACKOFF_MAX = 3600.0


def _state(conn) -> dict:
    row = conn.execute("SELECT * FROM billed_state WHERE id = 1").fetchone()
    if row is None:
        return {"last_ok": 0.0, "next_try": 0.0, "step": 0.0, "refreshed_at": 0.0, "error": ""}
    return dict(row)


def _save_state(conn, **fields: object) -> None:
    keys = ("last_ok", "next_try", "step", "refreshed_at", "error")
    current = _state(conn)
    current.update({k: v for k, v in fields.items() if k in keys})
    conn.execute(
        "INSERT INTO billed_state (id, last_ok, next_try, step, refreshed_at, error) "
        "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "last_ok=excluded.last_ok, next_try=excluded.next_try, step=excluded.step, "
        "refreshed_at=excluded.refreshed_at, error=excluded.error",
        tuple(current[k] for k in keys),
    )
    conn.commit()


def stored(conn) -> Billed:
    """Every cached day, oldest first."""
    rows = conn.execute("SELECT day, dollars FROM billed_days ORDER BY day").fetchall()
    at = _state(conn)["refreshed_at"]
    return Billed(
        tuple(Day(r["day"], r["dollars"]) for r in rows),
        datetime.fromtimestamp(at, UTC) if at else None,
    )


def _first_open_day(conn, now: datetime) -> datetime:
    """Where the next fetch starts: the day after the last final one, else the floor.

    A day is re-fetched until the API's horizon passes its end, because a day's cost is
    revised upward as late data lands. Once final it is never asked for again, which is
    what keeps the usual refresh to one chunk instead of twelve.
    """
    floor = max(EARLIEST, now - MAX_LOOKBACK)
    row = conn.execute("SELECT max(day) AS day FROM billed_days WHERE final = 1").fetchone()
    if not row or not row["day"]:
        return floor
    last = datetime.strptime(row["day"], "%Y-%m-%d").replace(tzinfo=UTC)
    return max(floor, last + timedelta(days=1))


def _write(conn, found: Billed, now: datetime) -> None:
    horizon = found.refreshed_at
    rows = [
        (d.day, d.dollars, 1 if _is_final(d.day, horizon) else 0, now.timestamp())
        for d in found.days
    ]
    conn.executemany(
        "INSERT INTO billed_days (day, dollars, final, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET dollars=excluded.dollars, final=excluded.final, "
        "fetched_at=excluded.fetched_at",
        rows,
    )
    conn.commit()


def _is_final(day: str, horizon: datetime | None) -> bool:
    """Whether the API's data horizon has passed the end of ``day``."""
    if horizon is None:
        return False
    end = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return horizon >= end


def refresh(conn, cwd: str | None = None, now: datetime | None = None) -> Billed:
    """The cached billed days, fetching what is missing when it is time to.

    Never raises. A failed fetch is recorded with a widening backoff and the cache is
    returned as it stands, because a stale billed figure beside a live computed one is
    worth more than an empty row.
    """
    at = now or datetime.now(UTC)
    state = _state(conn)
    if at.timestamp() < max(state["next_try"], state["last_ok"] + REFRESH):
        return stored(conn)
    key = admin_key(cwd)
    if not key:
        return stored(conn)
    try:
        found = fetch(key, _first_open_day(conn, at), at)
    except BillingError as e:
        step = min(max(state["step"] * 2, BACKOFF_FIRST), BACKOFF_MAX)
        _save_state(conn, next_try=at.timestamp() + step, step=step, error=str(e))
        return stored(conn)
    _write(conn, found, at)
    _save_state(
        conn,
        last_ok=at.timestamp(),
        next_try=0.0,
        step=0.0,
        refreshed_at=found.refreshed_at.timestamp() if found.refreshed_at else 0.0,
        error="",
    )
    return stored(conn)


def last_error(conn) -> str:
    """Why the last attempt failed, or '' when it did not. For `cabildo doctor`."""
    return str(_state(conn)["error"])
