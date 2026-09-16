"""One server, two modules: the bus tools and the issues/agenda tools are served
by a single `cabildo` FastMCP. These guard that the merge happens, is total, and
cannot silently drop a tool to a name collision.
"""

from __future__ import annotations

from cabildo import issues_mcp, mcp_server


def _names() -> set[str]:
    return set(mcp_server.mcp._tool_manager._tools)


def test_one_server_carries_bus_issues_and_agenda_tools():
    names = _names()
    assert {"whoami", "send", "inbox"} <= names  # bus
    assert {"board", "file_issue", "check_issues"} <= names  # issues
    assert {"agenda_board", "agenda_add", "agenda_done"} <= names  # agenda


def test_every_issue_tool_reaches_the_unified_server():
    assert set(issues_mcp.mcp._tool_manager._tools) <= _names()


def test_merge_is_idempotent_and_collision_free():
    before = dict(mcp_server.mcp._tool_manager._tools)
    mcp_server._merge_issue_tools()  # merging twice must not raise or change anything
    assert mcp_server.mcp._tool_manager._tools == before
