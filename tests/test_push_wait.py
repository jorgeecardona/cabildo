"""`cabildo wait` (GitLab #37): the blocking feed that replaces the mod's
once-per-second `pull` spawn. It must wake on a row another connection commits,
honour the cursor, and always exit (timeout, parent death, /clear rebind).
"""

from __future__ import annotations

import json
import threading
import time

from cabildo import cli, store

from .conftest import make_session

FAST = ["--interval", "0.01"]


def _post_later(delay: float, **kw) -> threading.Thread:
    def run() -> None:
        time.sleep(delay)
        c = store.connect()
        store.post_message(c, **kw)
        c.close()

    t = threading.Thread(target=run)
    t.start()
    return t


def test_wait_returns_a_row_committed_by_another_connection(conn, capsys):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    t = _post_later(0.1, from_session="A", from_handle="alice", body="hi B", to_session="B")
    started = time.monotonic()
    assert cli.main(["wait", "--session", "B", "--after", "0", "--timeout", "5", *FAST]) == 0
    t.join()
    assert time.monotonic() - started < 2, "woke on the commit, not at the timeout"
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "B"
    assert [m["preview"] for m in out["messages"]] == ["hi B"]
    assert out["cursor"] == out["messages"][0]["id"]


def test_wait_returns_at_once_when_rows_already_exist_past_the_cursor(conn, capsys):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    m1 = store.post_message(conn, from_session="A", from_handle="alice", body="1", to_session="B")
    m2 = store.post_message(conn, from_session="A", from_handle="alice", body="2", to_session="B")
    assert cli.main(["wait", "--session", "B", "--after", str(m1), "--timeout", "5", *FAST]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in out["messages"]] == [m2], "no lost wakeup, no replay before --after"


def test_wait_times_out_with_no_messages(conn, capsys):
    make_session(conn, "B")
    assert cli.main(["wait", "--session", "B", "--after", "0", "--timeout", "0.1", *FAST]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"session_id": "B", "cursor": 0, "messages": []}


def test_wait_ignores_rows_not_addressed_to_me(conn, capsys):
    make_session(conn, "A", handle="alice")
    make_session(conn, "B", handle="bob")
    make_session(conn, "C", handle="carol")
    t = _post_later(0.05, from_session="A", from_handle="alice", body="for C", to_session="C")
    assert cli.main(["wait", "--session", "B", "--after", "0", "--timeout", "0.4", *FAST]) == 0
    t.join()
    assert json.loads(capsys.readouterr().out)["messages"] == []


def test_wait_exits_silently_when_its_parent_dies(conn, capsys, monkeypatch):
    make_session(conn, "B")
    pids = iter([100, 100, 1])
    monkeypatch.setattr(cli.os, "getppid", lambda: next(pids, 1))
    started = time.monotonic()
    assert cli.main(["wait", "--session", "B", "--timeout", "5", *FAST]) == 0
    assert time.monotonic() - started < 2
    assert capsys.readouterr().out == ""


def test_wait_reports_a_rebind_after_clear(conn, capsys, monkeypatch):
    make_session(conn, "old")
    make_session(conn, "new")
    monkeypatch.setenv("CABILDO_SESSION_ID", "new")
    monkeypatch.setattr(cli, "WAIT_REBIND_S", 0.0)
    assert cli.main(["wait", "--session", "old", "--timeout", "5", *FAST]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"rebound": True, "session_id": "new", "cursor": store.max_message_id(conn)}


def test_wait_refuses_an_unknown_session(conn, capsys):
    assert cli.main(["wait", "--session", "ghost", "--timeout", "0.1"]) == 1
    assert "no such session" in capsys.readouterr().err
