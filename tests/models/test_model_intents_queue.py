"""Model-driven check of the intents queue: serialized draining, no starvation.

The daemon is a single worker that drains the intents queue one at a time
(take_next_intent flips pending->running atomically; the action runs; finish_intent
sets a terminal status; then the loop takes the next). Two properties matter:

  * serialization -- never two intents running at once (why land can't race land).
  * no starvation -- every queued intent is eventually processed.

The single-worker model proves both. A hypothetical concurrent-worker variant (take
ignoring whether one is already running) is shown to break serialization, which is
why the daemon is single-threaded by design.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

pytest.importorskip("musil")  # dev-only, needs Python >=3.12
from musil import Action, Model, check, check_liveness


@dataclass(frozen=True)
class Queue:
    a: str = "pending"  # intent A: pending | running | done
    b: str = "pending"  # intent B
    running: bool = False  # single worker: at most one intent in flight


ONE_AT_A_TIME = {"one-at-a-time": lambda s: not (s.a == "running" and s.b == "running")}


def _all_done(s: Queue) -> bool:
    return s.a == "done" and s.b == "done"


def _idle_enabled(s: Queue) -> bool:
    return not s.running and s.a != "pending" and s.b != "pending"


# The drained state has nothing to do; a self-loop keeps it from being a "deadlock".
IDLE = Action("idle", _idle_enabled, lambda s: s)


def _serial_actions(k: str) -> list[Action]:
    return [
        Action(
            f"take_{k}",
            lambda s, k=k: getattr(s, k) == "pending" and not s.running,
            lambda s, k=k: replace(s, running=True, **{k: "running"}),
        ),
        Action(
            f"finish_{k}",
            lambda s, k=k: getattr(s, k) == "running",
            lambda s, k=k: replace(s, running=False, **{k: "done"}),
        ),
    ]


def _concurrent_take(k: str) -> list[Action]:
    # Buggy: take ignores whether a worker is already running.
    return [
        Action(
            f"take_{k}",
            lambda s, k=k: getattr(s, k) == "pending",
            lambda s, k=k: replace(s, running=True, **{k: "running"}),
        ),
        Action(
            f"finish_{k}",
            lambda s, k=k: getattr(s, k) == "running",
            lambda s, k=k: replace(s, running=False, **{k: "done"}),
        ),
    ]


def test_single_worker_serializes_intents():
    model = Model(
        init=Queue(),
        actions=[*_serial_actions("a"), *_serial_actions("b"), IDLE],
        invariants=ONE_AT_A_TIME,
    )
    result = check(model)
    assert result.ok, f"{result.invariant}: {[step.action for step in result.trace]}"


def test_concurrent_workers_would_break_serialization():
    model = Model(
        init=Queue(),
        actions=[*_concurrent_take("a"), *_concurrent_take("b"), IDLE],
        invariants=ONE_AT_A_TIME,
    )
    result = check(model)
    assert not result.ok
    assert result.invariant == "one-at-a-time"


def test_no_intent_starves_under_fair_processing():
    model = Model(
        init=Queue(),
        actions=[*_serial_actions("a"), *_serial_actions("b"), IDLE],
    )
    result = check_liveness(
        model, goal=_all_done, fair=["take_a", "finish_a", "take_b", "finish_b"]
    )
    assert result.ok, getattr(result, "kind", None)
