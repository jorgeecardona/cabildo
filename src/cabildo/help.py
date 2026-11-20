"""The guide pages of cabildo, for the ``help`` MCP tool.

An agent calls ``help`` to learn how to use cabildo well. The pages are the Markdown files of
``docs/``: the same text the documentation site shows. A wheel carries them in
``cabildo/_bundled_docs/``, and a repo checkout has them in ``docs/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# This package's own directory. The pages sit beside it in a wheel (``_bundled_docs/``) or two
# levels up in a repo checkout (``../docs``).
_PKG_DIR = Path(__file__).resolve().parent
START = "start"
_PAGE_NAMES = {"using-cabildo": START}  # file stem -> the name an agent asks for
_NOT_TOPICS = frozenset({"index", "changelog"})
_MAX_HITS = 10


@dataclass(frozen=True)
class Topic:
    """One guide page."""

    name: str
    title: str
    summary: str
    path: Path


@dataclass(frozen=True)
class Hit:
    """One line of a page that matched a search."""

    topic: str
    heading: str
    line: str


def docs_dir() -> Path | None:
    """The folder that holds the guide pages, or None when it cannot be found.

    Checked in order: ``CABILDO_DOCS_DIR`` (an explicit override), the copy shipped inside the
    package, then ``docs/`` of a repo checkout.
    """
    override = os.environ.get("CABILDO_DOCS_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    for candidate in (_PKG_DIR / "_bundled_docs", _PKG_DIR.parents[1] / "docs"):
        if candidate.is_dir():
            return candidate
    return None


def _title_and_summary(text: str, fallback: str) -> tuple[str, str]:
    title, summary = fallback, ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            for after in lines[i + 1 :]:
                if after.strip() and not after.startswith("#"):
                    summary = after.strip()
                    break
            break
    return title, summary


def topics(root: Path) -> list[Topic]:
    """The pages of ``root``, the start guide first and the rest in name order."""
    found: list[Topic] = []
    for path in sorted(root.glob("*.md")):
        if path.stem in _NOT_TOPICS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        name = _PAGE_NAMES.get(path.stem, path.stem)
        title, summary = _title_and_summary(text, name)
        found.append(Topic(name, title, summary, path))
    return sorted(found, key=lambda t: (t.name != START, t.name))


def read(root: Path, name: str) -> str | None:
    """The text of the page called ``name``, or None when there is no such page."""
    for topic in topics(root):
        if topic.name == name.strip().lower():
            try:
                return topic.path.read_text(encoding="utf-8")
            except OSError:
                return None
    return None


def search(root: Path, query: str, limit: int = _MAX_HITS) -> list[Hit]:
    """The lines of the pages that hold every word of ``query``, with the heading above each.

    Case does not matter. At most ``limit`` lines come back, in page order.
    """
    words = query.lower().split()
    if not words:
        return []
    hits: list[Hit] = []
    for topic in topics(root):
        heading = topic.title
        in_code = False
        for line in topic.path.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                in_code = not in_code
            elif line.startswith("#") and not in_code:
                heading = line.lstrip("# ").strip()
            low = line.lower()
            if line.strip() and all(w in low for w in words):
                hits.append(Hit(topic.name, heading, line.strip()))
                if len(hits) >= limit:
                    return hits
    return hits
