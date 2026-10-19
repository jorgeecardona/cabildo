"""Durable decisions: record_decision writes, read_decisions (list_decisions) reads them back,
newest first, filterable by project, kind, and a substring query.
"""

from __future__ import annotations

from cabildo import store


def _decision(conn, title, **kw):
    return store.add_decision(conn, title=title, project_name=kw.pop("project_name", "alpha"), **kw)


def test_decisions_newest_first(conn):
    _decision(conn, "chose sqlite")
    _decision(conn, "chose WAL mode")
    rows = store.list_decisions(conn, project_name="alpha")
    assert [r["title"] for r in rows] == ["chose WAL mode", "chose sqlite"]


def test_decisions_scoped_by_project(conn):
    _decision(conn, "alpha decision", project_name="alpha")
    _decision(conn, "beta decision", project_name="beta")
    assert [r["title"] for r in store.list_decisions(conn, project_name="alpha")] == [
        "alpha decision"
    ]
    assert [r["title"] for r in store.list_decisions(conn, project_name="beta")] == [
        "beta decision"
    ]


def test_decisions_query_matches_title_body_refs_tags(conn):
    _decision(conn, "auth uses JWT", body="rejected sessions", refs="src/auth.py", tags="security")
    _decision(conn, "cache is LRU", body="fixed size", refs="src/cache.py", tags="perf")
    assert [r["title"] for r in store.list_decisions(conn, query="jwt")] == ["auth uses JWT"]
    assert [r["title"] for r in store.list_decisions(conn, query="rejected sessions")] == [
        "auth uses JWT"
    ]
    assert [r["title"] for r in store.list_decisions(conn, query="src/cache.py")] == [
        "cache is LRU"
    ]
    assert [r["title"] for r in store.list_decisions(conn, query="perf")] == ["cache is LRU"]


def test_decisions_filter_by_kind(conn):
    _decision(conn, "a decision", kind="decision")
    _decision(conn, "a warning", kind="warning")
    assert [r["title"] for r in store.list_decisions(conn, kind="warning")] == ["a warning"]


def test_decisions_respects_limit(conn):
    for i in range(5):
        _decision(conn, f"decision {i}")
    assert len(store.list_decisions(conn, limit=2)) == 2
