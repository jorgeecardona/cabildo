"""Forecast of a subscription limit: how likely it is to reach 100% before its reset.

Pure functions, no I/O. Two parts:

- ``fit_weights`` learns how many percent of the limit one token of each class costs, from the
  percent samples of the API and the tokens in the transcripts.
- ``simulate`` builds possible futures from real past hours (a block bootstrap) and counts how
  many of them reach 100% before the reset.

The token weights of the limit are not published. A fit whose increments do not follow the
tokens has ``usable`` False, and a caller must not show a forecast from it.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import quantiles

HOUR = 3600.0
WEEK_HOURS = 7 * 24
SWEEPS = 2000
TOLERANCE = 1e-12
MIN_GAP = HOUR  # shorter increments are mostly the rounding of whole percents
MIN_INCREMENTS = 24
MIN_R2 = 0.9
BLOCK_HOURS = 6  # keeps the runs of work and of sleep inside real history
PATHS = 2000
FULL = 100.0


@dataclass(frozen=True)
class Tokens:
    """Token counts by class."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def row(self) -> tuple[float, float, float, float]:
        """The counts in the order of ``Weights.row``."""
        return (
            float(self.input),
            float(self.output),
            float(self.cache_read),
            float(self.cache_write),
        )


@dataclass(frozen=True)
class Weights:
    """Percent of the limit per token of each class. Never negative."""

    input: float
    output: float
    cache_read: float
    cache_write: float

    def row(self) -> tuple[float, float, float, float]:
        """The weights in the order of ``Tokens.row``."""
        return (self.input, self.output, self.cache_read, self.cache_write)

    def percent(self, tokens: Tokens) -> float:
        """The percent of the limit that ``tokens`` cost."""
        return sum(w * t for w, t in zip(self.row(), tokens.row(), strict=True))


@dataclass(frozen=True)
class Observation:
    """One percent sample, with the local tokens since its window started."""

    resets_at: float
    at: float
    percent: float
    tokens: Tokens


@dataclass(frozen=True)
class Fit:
    """The learned weights and how well they explain the percent samples.

    ``unexplained`` is the percent per window that local tokens do not explain, such as use on
    claude.ai or on another machine, keyed by ``resets_at``. ``r2`` is measured on increments
    between samples, because two cumulative series that only rise correlate even when the
    weights are wrong. It is None with fewer than ``MIN_INCREMENTS`` increments.
    """

    weights: Weights
    unexplained: Mapping[float, float]
    r2: float | None
    increments: int

    @property
    def usable(self) -> bool:
        """True when the fit explains enough increments to base a forecast on."""
        return self.r2 is not None and self.r2 >= MIN_R2


def _nonnegative_least_squares(rows: Sequence[Sequence[float]], ys: Sequence[float]) -> list[float]:
    """Solve min ||Xb - y||^2 with b >= 0 by cyclic coordinate descent.

    Columns are scaled first, because token classes differ by orders of magnitude. A column
    that is all zero gets 0.
    """
    size = len(rows[0])
    scale = [max((abs(r[j]) for r in rows), default=0.0) or 1.0 for j in range(size)]
    rows = [[v / s for v, s in zip(r, scale, strict=True)] for r in rows]
    b = [0.0] * size
    residual = list(ys)
    norms = [sum(r[j] ** 2 for r in rows) for j in range(size)]
    for _ in range(SWEEPS):
        moved = 0.0
        for j in range(size):
            if norms[j] == 0:
                continue
            step = sum(r[j] * e for r, e in zip(rows, residual, strict=True)) / norms[j]
            new = max(0.0, b[j] + step)
            delta = new - b[j]
            if delta:
                b[j] = new
                residual = [e - r[j] * delta for r, e in zip(rows, residual, strict=True)]
                moved = max(moved, delta**2 * norms[j])
        if moved <= TOLERANCE:
            break
    return [v / s for v, s in zip(b, scale, strict=True)]


def _increments(obs: Sequence[Observation]) -> list[tuple[Tokens, float]]:
    """(tokens, percent) between samples of one window at least ``MIN_GAP`` apart."""
    found: list[tuple[Tokens, float]] = []
    last: Observation | None = None
    for o in sorted(obs, key=lambda o: (o.resets_at, o.at)):
        if last is None or last.resets_at != o.resets_at:
            last = o
        elif o.at - last.at >= MIN_GAP:
            found.append(
                (
                    Tokens(
                        o.tokens.input - last.tokens.input,
                        o.tokens.output - last.tokens.output,
                        o.tokens.cache_read - last.tokens.cache_read,
                        o.tokens.cache_write - last.tokens.cache_write,
                    ),
                    o.percent - last.percent,
                )
            )
            last = o
    return found


def _r2(weights: Weights, increments: Sequence[tuple[Tokens, float]]) -> float | None:
    if len(increments) < MIN_INCREMENTS:
        return None
    actual = [p for _, p in increments]
    mean = sum(actual) / len(actual)
    total = sum((p - mean) ** 2 for p in actual)
    if total == 0:
        return None
    error = sum((p - weights.percent(t)) ** 2 for t, p in increments)
    return 1 - error / total


def fit_weights(obs: Sequence[Observation]) -> Fit | None:
    """The weights that best explain the increments of ``obs``; None without an increment.

    Fitting on increments keeps the classes apart: their cumulative totals all rise together,
    so a fit on totals cannot tell them apart.
    """
    increments = _increments(obs)
    if not increments:
        return None
    b = _nonnegative_least_squares([t.row() for t, _ in increments], [p for _, p in increments])
    weights = Weights(*b)
    gaps: dict[float, list[float]] = {}
    for o in obs:
        gaps.setdefault(o.resets_at, []).append(o.percent - weights.percent(o.tokens))
    return Fit(
        weights,
        {w: max(0.0, sum(g) / len(g)) for w, g in gaps.items()},
        _r2(weights, increments),
        len(increments),
    )


def hour_of_week(hour_start: float) -> int:
    """The local hour of the week of an epoch time: 0 is Monday 00:00."""
    t = datetime.fromtimestamp(hour_start)
    return t.weekday() * 24 + t.hour


@dataclass(frozen=True)
class History:
    """Percent burned per past hour, in whole hours from ``start``. An idle hour is 0."""

    start: float
    burn: tuple[float, ...]


def history_of(hourly: Mapping[float, float], now: float) -> History | None:
    """``hourly`` (percent by hour start) as an unbroken series that ends before ``now``."""
    if not hourly:
        return None
    start = min(hourly)
    count = int((now - start) // HOUR)
    if count <= 0:
        return None
    return History(start, tuple(hourly.get(start + i * HOUR, 0.0) for i in range(count)))


@dataclass(frozen=True)
class Forecast:
    """What the simulated futures say at the reset.

    ``cutoff_*`` are epoch times among the paths that reached 100%, None when none did.
    ``final_*`` is the percent at the reset, not capped at 100.
    """

    p_cutoff: float
    final_p10: float
    final_p50: float
    final_p90: float
    cutoff_p10: float | None
    cutoff_p50: float | None
    cutoff_p90: float | None
    paths: int


def _candidates(history: History) -> dict[int, list[int]]:
    """Indexes in the history where a whole block starts, by hour of the week."""
    found: dict[int, list[int]] = {}
    for i in range(len(history.burn) - BLOCK_HOURS + 1):
        found.setdefault(hour_of_week(history.start + i * HOUR), []).append(i)
    return found


def simulate(
    history: History,
    percent: float,
    now: float,
    resets_at: float,
    *,
    task_percent: float = 0.0,
    paths: int = PATHS,
    seed: int = 0,
) -> Forecast | None:
    """Simulate from ``now`` to ``resets_at``; None when history has no whole block.

    Each path joins blocks of ``BLOCK_HOURS`` taken from past weeks at the same hour of the
    week. A slot with no such block in history falls back to any block. ``task_percent`` is
    added at ``now``, to answer "what if I start this task?". The fixed ``seed`` makes every
    reader of the same data show the same numbers.
    """
    candidates = _candidates(history)
    if not candidates:
        return None
    every = [i for found in candidates.values() for i in found]
    rng = random.Random(seed)  # noqa: S311 - a simulation, not security
    hours = max(0, int((resets_at - now) // HOUR))
    finals: list[float] = []
    cutoffs: list[float] = []
    for _ in range(paths):
        level = percent + task_percent
        crossed = now if level >= FULL else None
        for hour in range(0, hours, BLOCK_HOURS):
            at = now + hour * HOUR
            start = rng.choice(candidates.get(hour_of_week(at)) or every)
            for k in range(min(BLOCK_HOURS, hours - hour)):
                level += history.burn[start + k]
                if crossed is None and level >= FULL:
                    crossed = at + (k + 1) * HOUR
        finals.append(level)
        if crossed is not None:
            cutoffs.append(crossed)
    p10, p50, p90 = _deciles(finals)
    c10, c50, c90 = _deciles(cutoffs) if cutoffs else (None, None, None)
    return Forecast(len(cutoffs) / paths, p10, p50, p90, c10, c50, c90, paths)


def _deciles(values: Sequence[float]) -> tuple[float, float, float]:
    """The 10th, 50th and 90th percentile of ``values``."""
    if len(values) == 1:
        return values[0], values[0], values[0]
    cuts = quantiles(values, n=10, method="inclusive")
    return cuts[0], cuts[4], cuts[8]
