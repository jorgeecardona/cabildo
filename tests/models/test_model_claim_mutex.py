"""Model-driven check of cabildo's advisory-claim mutual exclusion.

cabildo agents coordinate a shared action (push, editing a file) by taking an
exclusive advisory claim on a resource (store.acquire_claim). The safety property
is: at most one agent holds a given resource at a time. This models two agents
racing for one claim and asks musil to prove it.

Two models, one lesson:
  * atomic acquire -- the real design, one serialized write (the broker's single
    connection + acquire_claim's INSERT ... ON CONFLICT). Mutual exclusion holds.
  * naive check-then-set -- acquire split into "see it free" then "take it", so two
    agents can both see it free and both take it. musil finds the interleaving.

This is the spec cabildo's claim path must keep satisfying; if acquire ever stops
being atomic, the naive model is what it degrades into.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

pytest.importorskip("musil")  # dev-only, needs Python >=3.12
from musil import Action, Model, check


@dataclass(frozen=True)
class Claim:
    """One resource and two agents. ``holder`` is who holds the claim; each agent
    phase is ``idle``, ``checking`` (naive model only), or ``holding``.
    """

    holder: str | None = None
    a: str = "idle"
    b: str = "idle"


AGENTS = ("a", "b")

MUTEX = {
    # The property that matters: two agents never hold the resource at once.
    "mutual-exclusion": lambda s: not (s.a == "holding" and s.b == "holding"),
    # The claim record and the agents' belief agree.
    "holder-consistency": lambda s: (s.holder is None) == (s.a != "holding" and s.b != "holding"),
}


def _atomic_actions(agent: str) -> list[Action]:
    """Acquire is a single step: check-free-and-take, indivisible."""
    return [
        Action(
            f"acquire_{agent}",
            lambda s, a=agent: s.holder is None and getattr(s, a) == "idle",
            lambda s, a=agent: replace(s, holder=a, **{a: "holding"}),
        ),
        Action(
            f"release_{agent}",
            lambda s, a=agent: getattr(s, a) == "holding" and s.holder == a,
            lambda s, a=agent: replace(s, holder=None, **{a: "idle"}),
        ),
    ]


def _naive_actions(agent: str) -> list[Action]:
    """Acquire split in two: see it free, then take it. The gap is the race."""
    return [
        Action(
            f"check_{agent}",
            lambda s, a=agent: s.holder is None and getattr(s, a) == "idle",
            lambda s, a=agent: replace(s, **{a: "checking"}),
        ),
        Action(
            f"commit_{agent}",
            lambda s, a=agent: getattr(s, a) == "checking",
            lambda s, a=agent: replace(s, holder=a, **{a: "holding"}),
        ),
        Action(
            f"release_{agent}",
            lambda s, a=agent: getattr(s, a) == "holding",
            lambda s, a=agent: replace(s, holder=None, **{a: "idle"}),
        ),
    ]


def test_atomic_claim_is_mutually_exclusive():
    model = Model(
        init=Claim(),
        actions=[act for agent in AGENTS for act in _atomic_actions(agent)],
        invariants=MUTEX,
    )
    result = check(model)
    assert result.ok, f"{result.invariant}: {[step.action for step in result.trace]}"
    assert result.states_explored >= 3  # both-idle, a-holds, b-holds at least


def test_naive_check_then_set_races():
    model = Model(
        init=Claim(),
        actions=[act for agent in AGENTS for act in _naive_actions(agent)],
        invariants=MUTEX,
    )
    result = check(model)
    assert not result.ok
    assert result.kind == "invariant"
    assert result.invariant == "mutual-exclusion"
    # The counterexample is the interleaving: both agents check while it is free,
    # then both commit. So both checks appear in the shortest trace.
    actions = {step.action for step in result.trace}
    assert {"check_a", "check_b"} <= actions
