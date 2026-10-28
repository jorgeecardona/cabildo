"""``__version__`` is the single source of truth; hatchling must build from it."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

import cabildo


def test_version_matches_metadata() -> None:
    # __version__ in src/cabildo/__init__.py is the sole version literal; pyproject's version is
    # dynamic (`[tool.hatch.version] path = ...`), so hatchling reads the installed metadata back
    # from that same literal. This asserts the build backend is wired to it -- if they drift, the
    # dynamic-version wiring is broken.
    assert cabildo.__version__ == importlib.metadata.version("cabildo")


def test_pyproject_version_is_dynamic() -> None:
    # Guard the single source: pyproject must declare the version dynamically, never carry a static
    # `version = "..."` that could silently diverge from the literal in __init__.py.
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert "version" not in pyproject["project"]
    assert "version" in pyproject["project"]["dynamic"]
