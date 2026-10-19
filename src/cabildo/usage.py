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
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
