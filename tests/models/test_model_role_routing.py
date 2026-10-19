"""Model-driven check of cabildo's route-by-role fan-out (send_to_role).

send_to_role hands work to whoever holds a role: it resolves the set of active
agents whose role matches, then DMs each one. The correctness property is: a
message is delivered to an agent only if that agent holds the target role at
delivery time. An agent that has dropped the role must not receive role-targeted
work meant for its successor.

Two models, one lesson:
  * atomic route -- the real design: resolve-and-post run as one serialized step
    against current state (send_to_role reads agents_with_role and posts inside
    the broker's single connection). The property holds.
  * naive snapshot-then-post -- resolve captured into a snapshot, then a gap, then
    post to the stale snapshot. An agent that changes role in the gap gets a
    message it should no longer get. musil finds the interleaving.

This is the spec cabildo's routing path must keep satisfying; if send_to_role
ever resolves recipients in one transaction and posts in another, the naive model
is what it degrades into.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

pytest.importorskip("musil")  # dev-only, needs Python >=3.12
from musil import Action, Model, check

TARGET = "reviewer"


@dataclass(frozen=True)
class Routing:
    """One router (agent a) fanning out to role TARGET, and one peer (agent b)
    whose role can change. ``b_role`` is b's current role; ``snapshot`` is the
    recipient set the naive router captured; ``phase`` tracks the route; and
    ``bad_delivery`` records a message that pushed on a non-holder.
    """

    b_role: str = TARGET
    snapshot: tuple[str, ...] | None = None
    phase: str = "idle"
    bad_delivery: bool = False


NO_MISROUTE = {
    # The property that matters: role-targeted work never pushes on an agent that
    # does not hold the role when the message is posted.
    "no-misroute": lambda s: not s.bad_delivery,
}


def _atomic_actions() -> list[Action]:
    """Resolve-and-post is a single step: recipients are read and messaged
    indivisibly, so the set posted to is always the set that holds the role now.
    """
    return [
        Action(
            "b_change_role",
            lambda s: s.phase == "idle",
            lambda s: replace(s, b_role="none" if s.b_role == TARGET else TARGET),
        ),
        Action(
            "route_atomic",
            lambda s: s.phase == "idle",
            # Deliver to b iff b holds the role in this same step; a holder is the
            # only recipient, so no bad delivery is possible.
            lambda s: replace(s, phase="done"),
        ),
    ]


def _naive_actions() -> list[Action]:
    """Resolve into a snapshot, then (after a gap) post to it. The gap is the
    race: b may drop the role between snapshot and post.
    """
    return [
        Action(
            "snapshot",
            lambda s: s.phase == "idle",
            lambda s: replace(s, phase="routing", snapshot=(("b",) if s.b_role == TARGET else ())),
        ),
        Action(
            "b_change_role",
            # b can flip role at any time, including mid-route.
            lambda s: s.phase in ("idle", "routing"),
            lambda s: replace(s, b_role="none" if s.b_role == TARGET else TARGET),
        ),
        Action(
            "post",
            lambda s: s.phase == "routing",
            # Deliver to the captured snapshot; if b is in it but no longer holds
            # the role, that is a misrouted delivery.
            lambda s: replace(
                s,
                phase="done",
                bad_delivery=("b" in (s.snapshot or ()) and s.b_role != TARGET),
            ),
        ),
    ]


def test_atomic_route_never_misroutes():
    model = Model(
        init=Routing(),
        actions=_atomic_actions(),
        invariants=NO_MISROUTE,
        # A finished route is a legitimate stop, not a deadlock.
        terminal=lambda s: s.phase == "done",
    )
    result = check(model)
    assert result.ok, f"{result.invariant}: {[step.action for step in result.trace]}"
    assert result.states_explored >= 3  # b=reviewer, b=none, and a done state


def test_naive_snapshot_then_post_misroutes():
    model = Model(
        init=Routing(),
        actions=_naive_actions(),
        invariants=NO_MISROUTE,
        terminal=lambda s: s.phase == "done",
    )
    result = check(model)
    assert not result.ok
    assert result.kind == "invariant"
    assert result.invariant == "no-misroute"
    # The counterexample is the interleaving: snapshot while b is a reviewer,
    # b drops the role, then post to the stale snapshot.
    actions = [step.action for step in result.trace]
    assert "snapshot" in actions
    assert "b_change_role" in actions
    assert "post" in actions
