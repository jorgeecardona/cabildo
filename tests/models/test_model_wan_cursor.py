"""Model-driven check of WAN sync convergence across a peer reset.

wan.py pulls a peer's messages with id greater than a per-peer cursor. If the
cursor is keyed only by address (host:port) and the peer's DB restarts from id 1
(a box reinstalled at the same IP, an SSH forward re-pointed), the local cursor
sits above the peer's new ids forever, so no new message ever syncs -- a silent
liveness failure (review finding #2).

The peer's generation is modeled as `node` (a DB reset mints a new one, which
handle_pull already returns). "Caught up" means the local has consumed the
peer's current generation. Under fair sync:
  * address-keyed cursor -- stalls after a reset, never catches up again.
  * node-keyed cursor -- notices the new generation, resets the cursor, reconverges.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

pytest.importorskip("musil")  # dev-only, needs Python >=3.12
from musil import Action, Model, check_liveness

CAP = 2  # bound on how many messages the peer holds, to keep the model finite


@dataclass(frozen=True)
class Sync:
    node: str = "n1"  # the peer's generation; a DB reset mints a new one
    count: int = 1  # the peer holds messages 1..count in this generation
    cursor: int = 1  # local: highest peer id merged
    known: str = "n1"  # the generation the cursor belongs to


def caught_up(s: Sync) -> bool:
    """The local has consumed the peer's current generation."""
    return s.known == s.node and s.cursor >= s.count


def _produce(s: Sync) -> Sync:
    return replace(s, count=s.count + 1)


def _reset(s: Sync) -> Sync:
    # DB reset: a new generation, ids restart at 1; the local cursor is untouched.
    return replace(s, node="n2", count=1)


def _sync_buggy(s: Sync) -> Sync:
    # Address-keyed: pull ids above the cursor, ignoring the peer's generation.
    return replace(s, cursor=max(s.cursor, s.count))


def _sync_fixed(s: Sync) -> Sync:
    # Node-keyed: a new generation resets the cursor before pulling.
    base = s if s.known == s.node else replace(s, cursor=0, known=s.node)
    return replace(base, cursor=max(base.cursor, base.count))


def _model(sync_apply) -> Model:
    return Model(
        init=Sync(),
        actions=[
            Action("produce", lambda s: s.count < CAP, _produce),
            Action("reset", lambda s: s.node == "n1", _reset),
            Action("sync", lambda s: True, sync_apply),
        ],
    )


def test_address_keyed_cursor_stalls_after_a_peer_reset():
    result = check_liveness(_model(_sync_buggy), goal=caught_up, everywhere=True, fair=["sync"])
    assert not result.ok


def test_node_keyed_cursor_reconverges_after_a_reset():
    result = check_liveness(_model(_sync_fixed), goal=caught_up, everywhere=True, fair=["sync"])
    assert result.ok, getattr(result, "kind", None)
