"""Token usage: measured from the transcripts, not guessed.

Why here and not ~/.claude/stats-cache.json: that file stopped being updated in
February and would happily throttle you on five-month-old numbers. The session
transcripts under ~/.claude/projects/**/*.jsonl are written as the work happens
and carry a per-message `usage` block with a timestamp and a model — an actual
time series, and the only local ground truth.

What this module deliberately does NOT do is claim to know your plan's limit.
Anthropic's weekly accounting is not published and the counter is known to
desync from the timer, so a module that hardcoded a threshold would be lying
with a straight face. It reports measured rates and answers "at this rate, when
do I cross N?" — you supply N.

    python -m cabildo.usage              # trend + forecast
    python -m cabildo.usage --budget 50e6 --window 7d
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import prices

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))

# Cache reads are billed at a small fraction of fresh input, so counting them
# raw would drown out everything else — this session alone read billions of
# cached tokens. These are cost-shaped weights, not plan-limit weights.
W_INPUT = 1.0
W_OUTPUT = 5.0
W_CACHE_WRITE = 1.25
W_CACHE_READ = 0.1


def _weighted(u: dict[str, Any]) -> float:
    return (
        u.get("input_tokens", 0) * W_INPUT
        + u.get("output_tokens", 0) * W_OUTPUT
        + u.get("cache_creation_input_tokens", 0) * W_CACHE_WRITE
        + u.get("cache_read_input_tokens", 0) * W_CACHE_READ
    )


def records(since: float) -> Iterator[dict[str, Any]]:
    """Yield {ts, model, project, session, in, out, cache_read, cache_write, weighted}
    for every assistant message newer than `since` (epoch seconds).
    """
    if not PROJECTS.is_dir():
        return
    for f in PROJECTS.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue  # whole file predates the window
        except OSError:
            continue
        try:
            with f.open(errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:  # cheap prefilter; most lines aren't
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    ts = d.get("timestamp")
                    if not u or not ts:
                        continue
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if t < since:
                        continue
                    yield {
                        "ts": t,
                        "model": msg.get("model") or "?",
                        "project": f.parent.name,
                        "session": f.stem,
                        "in": u.get("input_tokens", 0),
                        "out": u.get("output_tokens", 0),
                        "cache_read": u.get("cache_read_input_tokens", 0),
                        "cache_write": u.get("cache_creation_input_tokens", 0),
                        "weighted": _weighted(u),
                    }
        except OSError:
            continue


def _usage_row(line: bytes, path: Path) -> tuple[Any, ...] | None:
    """The ``usage_messages`` row of one transcript line, or None when it has no real usage.

    ``<synthetic>`` messages are Claude Code's own rate-limit rejections: no API call was made.
    """
    if b'"usage"' not in line:
        return None
    try:
        d = json.loads(line)
        msg = d["message"]
        u, message_id = msg["usage"], msg["id"]
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    model = msg.get("model") or "?"
    if not u or not message_id or model == "<synthetic>":
        return None
    # `cache_creation` splits the write by duration; an older transcript has only the total,
    # which was all 5-minute. Absent means 0, never "unknown": 0 prices the same as absent.
    by_ttl = u.get("cache_creation")
    long_write = by_ttl.get("ephemeral_1h_input_tokens", 0) if isinstance(by_ttl, dict) else 0
    return (
        message_id,
        ts,
        d.get("sessionId") or path.stem,
        d.get("agentId"),
        path.relative_to(PROJECTS).parts[0],
        model,
        u.get("input_tokens", 0),
        u.get("output_tokens", 0),
        u.get("cache_read_input_tokens", 0),
        u.get("cache_creation_input_tokens", 0),
        long_write,
        1 if u.get("speed") == "fast" else 0,
    )


def _ingest_file(conn: sqlite3.Connection, path: Path, offset: int) -> None:
    """Read ``path`` from ``offset`` into ``usage_messages``. A line without its newline is still
    being written, so the cursor stops before it. A file shorter than ``offset`` is read again.
    """
    with path.open("rb") as fh:
        if fh.seek(0, os.SEEK_END) < offset:
            offset = 0
        fh.seek(offset)
        rows = []
        for line in fh:
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            if (row := _usage_row(line, path)) is not None:
                rows.append(row)
    with conn:
        # streamed lines of one message can show a partial output count: keep the largest
        conn.executemany(
            "INSERT INTO usage_messages (message_id, ts, session_id, agent_id, project, model, "
            "input, output, cache_read, cache_write, cache_write_1h, fast) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            "input = max(input, excluded.input), output = max(output, excluded.output), "
            "cache_read = max(cache_read, excluded.cache_read), "
            "cache_write = max(cache_write, excluded.cache_write), "
            "cache_write_1h = max(cache_write_1h, excluded.cache_write_1h), "
            "fast = max(fast, excluded.fast)",
            rows,
        )
        conn.execute(
            "INSERT INTO usage_cursors (path, byte_offset) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET byte_offset = excluded.byte_offset",
            (str(path), offset),
        )


def ingest(conn: sqlite3.Connection) -> None:
    """Bring ``usage_messages`` up to date with every transcript, reading only new bytes."""
    if not PROJECTS.is_dir():
        return
    cursors = dict(conn.execute("SELECT path, byte_offset FROM usage_cursors").fetchall())
    for path in PROJECTS.rglob("*.jsonl"):
        offset = cursors.get(str(path), 0)
        try:
            if path.stat().st_size != offset:
                _ingest_file(conn, path, offset)
        except OSError:
            continue  # the file went away between the listing and the read


# --------------------------------------------------------------------------- #
# What it cost
# --------------------------------------------------------------------------- #
DAY = 86400.0


@dataclass(frozen=True)
class Window:
    """One period's consumption: tokens by kind, and what they cost.

    ``cost`` covers the priced part only, and ``unpriced`` says how many tokens are not in
    it -- a model the price table does not know, or a fast-mode reply, which bills at rates
    the table does not carry. Reading ``cost`` as the whole bill while ``unpriced`` is
    non-zero understates it, which is why the renderer shows both.
    """

    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    cost: float = 0.0
    unpriced: int = 0

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read


@dataclass(frozen=True)
class Spend:
    """What this machine's transcripts say was consumed, over three periods."""

    today: Window
    week: Window
    all_time: Window


#: `ts >= ?` per window. Today is local midnight, not 24 hours back: "today" is read off a
#: calendar, and a figure that silently dropped this morning's work at noon would be wrong
#: in the way nobody checks.
def _midnight(now: float) -> float:
    local = datetime.fromtimestamp(now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


_GROUPED = (
    "SELECT model, fast, (input + cache_read + cache_write) > ? AS long_prompt, "
    "sum(input) AS input, sum(output) AS output, sum(cache_read) AS cache_read, "
    "sum(cache_write) AS cache_write, sum(cache_write_1h) AS cache_write_1h "
    "FROM usage_messages WHERE ts >= ?{where} GROUP BY model, fast, long_prompt"
)


def window(
    conn: sqlite3.Connection, since: float, sessions: tuple[str, ...] | None = None
) -> Window:
    """Everything recorded at or after ``since``, priced.

    ``sessions`` narrows it to those session ids -- a conversation resumed twice has
    several, and a subagent's messages are recorded against the parent, so passing a
    conversation's ids gives that conversation's whole cost. None means every session.

    Grouping before pricing is exact, not an approximation: every rate is dollars per
    token, so the cost of a group equals the sum of its rows' costs as long as the group
    shares the model, the speed and the price tier -- which is what it is grouped by.
    """
    if sessions is not None and not sessions:
        return Window()
    where = "" if sessions is None else f" AND session_id IN ({','.join('?' * len(sessions))})"
    sql = _GROUPED.format(where=where)
    args: tuple = (prices.LONG_PROMPT_TOKENS, since, *(sessions or ()))
    totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    cost, unpriced = 0.0, 0
    for row in conn.execute(sql, args):
        write_1h = row["cache_write_1h"]
        write_5m = row["cache_write"] - write_1h
        for key in totals:
            totals[key] += row[key]
        rates = (
            None if row["fast"] else prices.price_of(row["model"], long_prompt=row["long_prompt"])
        )
        if rates is None:
            unpriced += row["input"] + row["output"] + row["cache_write"] + row["cache_read"]
            continue
        cost += (
            row["input"] * rates.input
            + write_5m * rates.cache_write_5m
            + write_1h * rates.cache_write_1h
            + row["cache_read"] * rates.cache_read
            + row["output"] * rates.output
        ) / prices.PER_MILLION
    return Window(**totals, cost=cost, unpriced=unpriced)


def spend(
    conn: sqlite3.Connection, now: float | None = None, sessions: tuple[str, ...] | None = None
) -> Spend:
    """Today, the last 7 days, and everything on record. Reads the DB; does not ingest.

    ``sessions`` narrows every window to one conversation; None covers the machine.
    """
    at = time.time() if now is None else now
    return Spend(
        today=window(conn, _midnight(at), sessions),
        week=window(conn, at - 7 * DAY, sessions),
        all_time=window(conn, 0.0, sessions),
    )


def summarize(since: float) -> dict[str, Any]:
    rows = list(records(since))
    by_hour: dict[int, float] = defaultdict(float)
    by_day: dict[str, float] = defaultdict(float)
    by_project: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    totals = defaultdict(float)
    for r in rows:
        h = int(r["ts"] // 3600)
        by_hour[h] += r["weighted"]
        by_day[datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")] += r["weighted"]
        by_project[r["project"]] += r["weighted"]
        by_model[r["model"]] += r["weighted"]
        for k in ("in", "out", "cache_read", "cache_write", "weighted"):
            totals[k] += r[k]
    return {
        "n": len(rows),
        "since": since,
        "totals": dict(totals),
        "by_hour": dict(by_hour),
        "by_day": dict(by_day),
        "by_project": dict(by_project),
        "by_model": dict(by_model),
        "sessions": len({r["session"] for r in rows}),
    }


def rate_per_hour(by_hour: dict[int, float], lookback_hours: int = 6) -> float:
    """Recent burn rate. Uses only hours that actually have activity: averaging
    over wall-clock would report a low rate simply because you slept, and then
    forecast that you'll never hit the limit.
    """
    if not by_hour:
        return 0.0
    newest = max(by_hour)
    live = [v for h, v in by_hour.items() if h > newest - lookback_hours and v > 0]
    return sum(live) / len(live) if live else 0.0


def forecast(spent: float, budget: float, rate_hr: float) -> dict[str, Any]:
    """When does `spent` cross `budget` at `rate_hr`?"""
    left = budget - spent
    if left <= 0:
        return {"state": "OVER", "pct": 100.0, "hours_left": 0.0, "eta": None}
    if rate_hr <= 0:
        return {"state": "IDLE", "pct": spent / budget * 100, "hours_left": None, "eta": None}
    hrs = left / rate_hr
    return {
        "state": "OK" if hrs > 24 else ("WARN" if hrs > 4 else "CRITICAL"),
        "pct": spent / budget * 100,
        "hours_left": hrs,
        "eta": (datetime.now() + timedelta(hours=hrs)).strftime("%a %H:%M"),
    }


def _human(n: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cabildo-usage", description=(__doc__ or "").split("\n")[0])
    p.add_argument("--days", type=float, default=7.0, help="window to scan")
    p.add_argument(
        "--budget",
        type=float,
        default=0.0,
        help="weighted-token budget for the window; enables the forecast",
    )
    args = p.parse_args(argv)

    since = time.time() - args.days * 86400
    t0 = time.time()
    s = summarize(since)
    tot = s["totals"]

    print(
        f"scanned {s['n']:,} messages from {s['sessions']} sessions "
        f"over {args.days:g}d in {time.time() - t0:.1f}s\n"
    )
    print(f"  output       {_human(tot.get('out', 0)):>8}")
    print(f"  input        {_human(tot.get('in', 0)):>8}")
    print(f"  cache write  {_human(tot.get('cache_write', 0)):>8}")
    print(f"  cache read   {_human(tot.get('cache_read', 0)):>8}")
    print(
        f"  weighted     {_human(tot.get('weighted', 0)):>8}   (out×{W_OUTPUT:g} "
        f"+ in×{W_INPUT:g} + cw×{W_CACHE_WRITE:g} + cr×{W_CACHE_READ:g})\n"
    )

    print("by day (weighted):")
    for d in sorted(s["by_day"])[-10:]:
        v = s["by_day"][d]
        bar = "█" * max(1, int(30 * v / max(s["by_day"].values())))
        print(f"  {d}  {_human(v):>7}  {bar}")

    print("\ntop projects:")
    for k, v in sorted(s["by_project"].items(), key=lambda x: -x[1])[:6]:
        print(f"  {k[:44]:<44} {_human(v):>8}")

    rate = rate_per_hour(s["by_hour"])
    print(f"\nburn rate (active hours, last 6h): {_human(rate)}/h")

    if args.budget:
        f = forecast(tot.get("weighted", 0), args.budget, rate)
        print(f"budget {_human(args.budget)} · used {f['pct']:.1f}% · state {f['state']}")
        if f["hours_left"] is not None:
            print(f"at this rate you cross it in {f['hours_left']:.1f}h → ~{f['eta']}")
    else:
        print("(pass --budget N to forecast when you cross it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
