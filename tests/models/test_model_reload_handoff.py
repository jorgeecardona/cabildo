"""Model-driven check of the reload handoff: no wedged Claude, no zombie, no lost descriptor.

`R` restarts the workspace in place. The process survives `exec`, so its children and its
open descriptors do too, and each pane hands its pseudo-terminal across rather than closing
it (`cabildo.workspace.attach`). The danger is not that the sequence is long; it is that it
can be interrupted at any point -- Claude exits mid-restart, `exec` fails, the attach raises
-- and each interruption leaves a child and a descriptor in some state.

The states that must never be reachable:

  * **a wedged Claude** -- still running, its pseudo-terminal still open, and nobody
    reading it. It blocks forever on a full buffer. This is strictly worse than the hangup
    a plain quit would have given it, which is why a descriptor that cannot be attached is
    closed rather than leaked;
  * **a closed descriptor the event loop still watches** -- the asyncio error a reload
    showed on Linux, whose selector keeps its own map of watched descriptors;
  * **a zombie** -- a child that exited and was never reaped by the process that is still
    its parent. This one is liveness, not safety: the reap is a thread the close starts,
    so it happens shortly after, never atomically with it. Asking for it as an invariant
    would demand an atomicity the code does not have, and musil says so;
  * **a lost descriptor** -- the restart is over and a master is open that no pane owns.

Two models, one lesson, as elsewhere in this directory:
  * the design as written -- the states above are unreachable;
  * a naive handoff -- one that lets the collector close the original descriptor, and
    that drops a pane it could not rebuild. musil finds both bad states and prints the
    interruption that reaches them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

pytest.importorskip("musil")  # dev-only, needs Python >=3.12
from musil import Action, Model, check, check_liveness

#: Where the restart has got to. `handed` is after a detach and before the new program
#: runs -- the window `exec` spans, where nothing is reading anything. `hosting` again
#: means the pane is back and draining its terminal; `closed` means the session is over.
PHASES = ("hosting", "handed", "restarted", "closed")


@dataclass(frozen=True)
class Reload:
    """One pane and its Claude, crossing a reload.

    `orig` is the pseudo-terminal master the running program holds; `dup` is the copy made
    to survive `exec`. `watched` is whether the event loop has a reader armed on `orig` --
    bittty arms one before each read, so a read cancelled by a detach leaves it armed.
    `reading` is whether anything is draining a master: a pane is, the gap around `exec`
    is not.
    """

    phase: str = "hosting"
    child: str = "alive"  # alive | exited | reaped
    orig: str = "open"  # open | closed
    dup: str = "none"  # none | open | closed
    watched: bool = False
    reading: bool = True
    written_down: bool = False  # the handoff names this pane

    @property
    def masters_open(self) -> int:
        return (self.orig == "open") + (self.dup == "open")


def wedged(s: Reload) -> bool:
    """Running, its terminal open, nobody reading, and nobody on the way to read it."""
    in_transit = s.phase in ("handed", "restarted") and s.written_down
    return s.child == "alive" and s.masters_open > 0 and not s.reading and not in_transit


SAFE = {
    # The one that matters: a Claude blocked on a terminal nobody drains.
    "no-wedged-claude": lambda s: not wedged(s),
    # The asyncio error on Linux: a closed descriptor the loop is still watching.
    "never-close-a-watched-descriptor": lambda s: not (s.orig == "closed" and s.watched),
    # Nothing left open once the session is over. While a pane hosts one, an open
    # master is the point, not a leak -- which is why this asks about `closed`.
    "no-lost-descriptor": lambda s: not (s.phase == "closed" and s.masters_open > 0),
}


def _child_actions() -> list[Action]:
    """Claude can exit at any moment, and the parent reaps it when it next looks."""
    return [
        Action(
            "claude_exits",
            lambda s: s.child == "alive",
            lambda s: replace(s, child="exited", reading=False),
        ),
        Action(
            "parent_reaps",
            lambda s: s.child == "exited",
            lambda s: replace(s, child="reaped"),
        ),
    ]


def _real_actions() -> list[Action]:
    """The handoff as written: release unwatches then closes, and a pane that cannot be
    rebuilt has its descriptor closed and its child hung up.
    """
    return [
        # bittty arms a reader before each read; the callback disarms it.
        Action(
            "arm_reader",
            lambda s: s.phase == "hosting" and s.reading and not s.watched,
            lambda s: replace(s, watched=True),
        ),
        Action(
            "read_completes",
            lambda s: s.watched and s.phase == "hosting",
            lambda s: replace(s, watched=False),
        ),
        Action(
            "detach",
            lambda s: s.phase == "hosting",
            # duplicate first, then release: unwatch, then close the original.
            lambda s: replace(
                s,
                phase="handed",
                dup="open",
                watched=False,
                orig="closed",
                reading=False,
                written_down=True,
            ),
        ),
        Action(
            "exec",
            lambda s: s.phase == "handed",
            lambda s: replace(s, phase="restarted"),
        ),
        Action(
            "attach",
            lambda s: s.phase == "restarted" and s.written_down and s.child == "alive",
            # back to hosting: a rebuilt pane is an ordinary pane, and can be reloaded again
            lambda s: replace(
                s, phase="hosting", orig="open", dup="none", reading=True, written_down=False
            ),
        ),
        # The child died during the restart, or the pane could not be rebuilt: close the
        # descriptor and hang the process group up, rather than leaving either behind.
        Action(
            "give_up",
            lambda s: s.phase == "restarted" and s.written_down,
            lambda s: replace(
                s,
                phase="closed",
                dup="closed",
                reading=False,
                written_down=False,
                child="reaped" if s.child != "alive" else "exited",
            ),
        ),
        # A quit ends the session the ordinary way: the terminal closes, the child is
        # hung up by that close, and the parent reaps it.
        Action(
            "quit",
            lambda s: s.phase == "hosting",
            lambda s: replace(
                s,
                phase="closed",
                orig="closed",
                dup="closed" if s.dup == "open" else s.dup,
                watched=False,
                reading=False,
                child="reaped" if s.child != "alive" else "exited",
            ),
        ),
    ]


def _naive_actions() -> list[Action]:
    """A handoff that leaves the two things to chance.

    `detach_and_forget` drops the terminal object with its reader still armed and lets the
    collector close the descriptor later. `drop_pane` abandons a pane it could not rebuild
    instead of closing its descriptor and hanging its child up.
    """
    keep = {"arm_reader", "read_completes", "exec", "attach", "quit"}
    actions = [a for a in _real_actions() if a.name in keep]
    return [
        *actions,
        Action(
            "detach_and_forget",
            lambda s: s.phase == "hosting",
            # the reader stays armed, and `orig` is closed later, by the collector
            lambda s: replace(s, phase="handed", dup="open", reading=False, written_down=True),
        ),
        Action(
            "collector_closes",
            lambda s: s.phase in ("handed", "restarted") and s.orig == "open",
            lambda s: replace(s, orig="closed"),
        ),
        Action(
            "drop_pane",
            lambda s: s.phase == "restarted" and s.written_down,
            lambda s: replace(s, phase="closed", written_down=False),
        ),
    ]


def _ended(s: Reload) -> bool:
    """The session is over and nothing is left: closed, reaped, no descriptor open.

    Declared terminal so musil reads it as the end of a run rather than a deadlock. It is
    the only state a run is allowed to stop in, which is itself part of the property.
    """
    return s.phase == "closed" and s.child == "reaped" and s.masters_open == 0


def _model(actions: list[Action]) -> Model:
    return Model(
        init=Reload(),
        actions=[*actions, *_child_actions()],
        invariants=SAFE,
        terminal=_ended,
    )


def test_the_handoff_as_written_never_wedges_leaks_or_orphans():
    result = check(_model(_real_actions()))
    assert result.ok, f"{result.invariant}: {[step.action for step in result.trace]}"
    assert result.states_explored > 10  # the interruptions really are explored


def test_a_claude_that_dies_mid_restart_is_reaped_and_its_terminal_closed():
    """The interruption that worried us: Claude exits between the detach and the attach."""
    result = check(_model(_real_actions()))
    assert result.ok


def test_letting_the_collector_close_the_descriptor_closes_a_watched_one():
    """The Linux asyncio error, as a reachable state rather than a flash on screen."""
    result = check(_model(_naive_actions()))
    assert not result.ok
    assert result.kind == "invariant"
    assert result.invariant in SAFE
    names = [step.action for step in result.trace]
    assert "detach_and_forget" in names


def test_abandoning_a_pane_wedges_its_claude():
    """A pane dropped without closing its descriptor leaves Claude blocked on a terminal
    nobody reads -- worse than the hangup a plain quit would have given it.
    """
    without_the_bug = [a for a in _naive_actions() if a.name != "detach_and_forget"]
    # keep the real detach so only the give-up path differs
    actions = [*without_the_bug, *[a for a in _real_actions() if a.name == "detach"]]
    result = check(_model(actions))
    assert not result.ok
    assert result.invariant in ("no-wedged-claude", "no-lost-descriptor")
    assert "drop_pane" in [step.action for step in result.trace]


def test_every_child_that_exits_is_eventually_reaped():
    """No permanent zombie. Liveness, not safety: `ClaudePane.close` reaps on a thread, so
    the reap trails the exit instead of coinciding with it, and the property is "eventually"
    rather than "always". `parent_reaps` is fair -- a scheduler that never ran it would be
    a model of a parent that never looks at its children again, which is not this one.
    """
    result = check_liveness(
        _model(_real_actions()),
        goal=lambda s: s.child != "exited",
        fair=["parent_reaps"],
        everywhere=True,
        goal_name="no-zombie",
    )
    # a liveness counterexample is a lasso: how it got there, then the cycle it sits in
    assert result.ok, f"{[s.action for s in result.stem]} then {[s.action for s in result.cycle]}"
