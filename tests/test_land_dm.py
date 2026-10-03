"""Land-failure DM ergonomics: the moon upgrade banner must not crowd out or
truncate the actual failing task (issue #22).
"""

from __future__ import annotations

from cabildo import actions

_GATE_OUT = """\
gate: lint + typecheck
▮▮▮▮ There's a new version of moon available, 2.5.6 (currently on 2.2.6)!
▮▮▮▮ OpenTelemetry, shared worktree cache, async experiments, and more!
▮▮▮▮ Learn more: https://moonrepo.dev/blog/moon-v2.5
▮▮▮▮ Run proto install moon 2.5.6 --pin to upgrade!
× Task web:lint failed
ERR_PNPM_RECURSIVE_EXEC_FIRST_FAIL  Command "tsc" not found
"""


def test_clean_gate_output_drops_the_moon_banner():
    cleaned = actions._clean_gate_output(_GATE_OUT)
    assert "new version of moon" not in cleaned
    assert "▮" not in cleaned
    assert "moonrepo.dev/blog" not in cleaned
    # The real content survives.
    assert "web:lint failed" in cleaned
    assert 'Command "tsc" not found' in cleaned


def test_gate_failure_reason_leads_with_the_failing_task():
    reason = actions._gate_failure_reason(1, _GATE_OUT)
    first_lines = reason.splitlines()[:4]
    joined = "\n".join(first_lines)
    # The failing task appears up top, before any banner noise.
    assert "web:lint failed" in joined
    assert "▮" not in reason
    assert reason.startswith("gate failed (rc=1)")


def test_gate_failure_reason_without_banner_is_still_fine():
    reason = actions._gate_failure_reason(2, "× Task api:test failed\nassert 1 == 2\n")
    assert "api:test failed" in reason.splitlines()[1]
