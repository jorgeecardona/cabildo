"""Model-driven check of the broker's connection transaction state.

The broker owns one sqlite connection shared across handler threads. A write op
opens a transaction; on success the store commits it, so at rest the connection
holds no open transaction. If a handler raises mid-write and the except does NOT
roll back, the transaction stays open -- and a later read pins a stale snapshot,
so every subsequent write fails with "database is locked". The broker is wedged
until restart (review finding #1, HIGH).

`tx` here is "an uncommitted transaction persists after the op returned" -- which
must always be False at rest. The buggy model (no rollback) reaches a wedged
state; the fixed model (except rolls back) never does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

pytest.importorskip("musil")  # dev-only, needs Python >=3.12
from musil import Action, Model, check

NEVER_WEDGED = {"never-wedged": lambda s: not s.tx}


@dataclass(frozen=True)
class Conn:
    tx: bool = False  # an uncommitted transaction left open after an op returned


def test_no_rollback_wedges_the_connection():
    # Buggy: a failed write leaves the transaction open and nothing clears it.
    model = Model(
        init=Conn(),
        actions=[
            Action("op_ok", lambda s: not s.tx, lambda s: replace(s, tx=False)),
            Action("op_fail", lambda s: not s.tx, lambda s: replace(s, tx=True)),
        ],
        invariants=NEVER_WEDGED,
    )
    result = check(model)
    assert not result.ok
    assert "op_fail" in [step.action for step in result.trace]


def test_rollback_on_failure_keeps_the_connection_clean():
    # Fixed: the except rolls back, so a failed write returns the connection to
    # no-open-transaction and the next op proceeds.
    model = Model(
        init=Conn(),
        actions=[
            Action("op_ok", lambda s: not s.tx, lambda s: replace(s, tx=False)),
            Action("op_fail_rollback", lambda s: not s.tx, lambda s: replace(s, tx=False)),
        ],
        invariants=NEVER_WEDGED,
    )
    result = check(model)
    assert result.ok, f"{result.invariant}: {[step.action for step in result.trace]}"
