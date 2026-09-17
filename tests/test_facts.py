"""Durable decisions: set_fact writes, catch_up (list_facts) reads them back,
newest first, filterable by project, kind, and a substring query.
"""

from __future__ import annotations

from cabildo import store


def _fact(conn, title, **kw):
    return store.add_fact(conn, title=title, project_name=kw.pop("project_name", "alpha"), **kw)


def test_facts_newest_first(conn):
    _fact(conn, "chose sqlite")
    _fact(conn, "chose WAL mode")
    rows = store.list_facts(conn, project_name="alpha")
    assert [r["title"] for r in rows] == ["chose WAL mode", "chose sqlite"]


def test_facts_scoped_by_project(conn):
    _fact(conn, "alpha decision", project_name="alpha")
    _fact(conn, "beta decision", project_name="beta")
    assert [r["title"] for r in store.list_facts(conn, project_name="alpha")] == ["alpha decision"]
    assert [r["title"] for r in store.list_facts(conn, project_name="beta")] == ["beta decision"]


def test_facts_query_matches_title_body_refs_tags(conn):
    _fact(conn, "auth uses JWT", body="rejected sessions", refs="src/auth.py", tags="security")
    _fact(conn, "cache is LRU", body="fixed size", refs="src/cache.py", tags="perf")
    assert [r["title"] for r in store.list_facts(conn, query="jwt")] == ["auth uses JWT"]
    assert [r["title"] for r in store.list_facts(conn, query="rejected sessions")] == [
        "auth uses JWT"
    ]
    assert [r["title"] for r in store.list_facts(conn, query="src/cache.py")] == ["cache is LRU"]
    assert [r["title"] for r in store.list_facts(conn, query="perf")] == ["cache is LRU"]


def test_facts_filter_by_kind(conn):
    _fact(conn, "a decision", kind="decision")
    _fact(conn, "a warning", kind="warning")
    assert [r["title"] for r in store.list_facts(conn, kind="warning")] == ["a warning"]


def test_facts_respects_limit(conn):
    for i in range(5):
        _fact(conn, f"decision {i}")
    assert len(store.list_facts(conn, limit=2)) == 2
