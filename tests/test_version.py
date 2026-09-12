"""``cabildo.__version__`` must equal the installed (pyproject) version."""

from __future__ import annotations

import importlib.metadata

import cabildo


def test_version_matches_metadata() -> None:
    # The literal in __init__.py exists for humans (`cabildo.__version__` at a REPL);
    # pyproject stays the release trigger. scripts/bump-version rewrites both, and this
    # test fails the suite if they ever drift.
    assert cabildo.__version__ == importlib.metadata.version("cabildo")
