"""The ``help`` tool serves the guide pages of docs/."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cabildo import help as guide
from cabildo import mcp_server as srv


@pytest.fixture
def pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "using-cabildo.md").write_text(
        "# Use cabildo well\n\nRead this first.\n\n## Push\n\nQueue a push.\n"
    )
    (tmp_path / "gateway.md").write_text(
        "# Gateway\n\nA local proxy.\n\n## Set it up\n\nRun `cabildo gateway`.\n"
    )
    (tmp_path / "index.md").write_text("# Home\n\nNot a topic.\n")
    (tmp_path / "changelog.md").write_text("# Changelog\n")
    monkeypatch.setenv("CABILDO_DOCS_DIR", str(tmp_path))
    return tmp_path


def call(**kw: Any) -> dict[str, Any]:
    return asyncio.run(srv.help_tool(**kw))


def test_topics_put_the_start_guide_first_and_leave_out_index_and_changelog(pages: Path) -> None:
    found = guide.topics(pages)
    assert [t.name for t in found] == ["start", "gateway"]
    assert found[0].title == "Use cabildo well" and found[0].summary == "Read this first."


def test_no_argument_returns_the_start_guide_and_the_topic_list(pages: Path) -> None:
    out = call()
    assert out["ok"] and out["topic"] == "start" and "Read this first." in out["text"]
    assert [t["name"] for t in out["topics"]] == ["start", "gateway", "tools"]


def test_a_topic_returns_its_page_and_an_unknown_one_lists_the_topics(pages: Path) -> None:
    assert "A local proxy." in call(topic="Gateway")["text"]
    bad = call(topic="nope")
    assert not bad["ok"] and "gateway" in [t["name"] for t in bad["topics"]]


def test_query_finds_lines_with_every_word_and_names_the_heading(pages: Path) -> None:
    hits = call(query="queue PUSH")["hits"]
    assert hits == [{"topic": "start", "heading": "Push", "line": "Queue a push."}]
    assert call(query="zzz")["hits"] == []


def test_query_ignores_headings_inside_code_blocks(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n\n## Real\n\n```text\n# not a heading\nfind me\n```\n")
    assert guide.search(tmp_path, "find")[0].heading == "Real"


def test_tools_lists_every_tool_with_its_first_line(pages: Path) -> None:
    text = call(topic="tools")["text"]
    assert "- help: How to use cabildo well." in text
    assert "- whoami:" in text and "- send:" in text


def test_a_missing_docs_folder_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CABILDO_DOCS_DIR", str(tmp_path / "nope"))
    assert call()["ok"] is False


def test_the_real_guide_has_the_pages_the_tool_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CABILDO_DOCS_DIR", raising=False)
    root = guide.docs_dir()
    assert root is not None
    names = {t.name for t in guide.topics(root)}
    assert {"start", "gateway", "channels", "roles", "browser", "crons"} <= names
