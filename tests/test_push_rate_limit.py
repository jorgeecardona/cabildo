"""The daemon refuses a push when one session already landed too many on one target (#39)."""

from __future__ import annotations

import json
import time

import pytest

from cabildo import actions


def _landed(conn, session, *, target="main", n=1, age=0.0):
    for _ in range(n):
        conn.execute(
            "INSERT INTO intents(created_at, from_session, action, params, status, finished_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                time.time(),
                session,
                "push",
                json.dumps({"cwd": "/x", "target": target}),
                "done",
                time.time() - age,
            ),
        )
    conn.commit()


def _push(tmp_path, session="s1", target="main"):
    ctx = actions.Ctx(worker="w", allow_push=True)
    params = {"cwd": str(tmp_path), "target": target, "_from_session": session}
    return actions.push(params, ctx)


def _refused(tmp_path, **kw):
    with pytest.raises(actions.Reject) as err:
        _push(tmp_path, **kw)
    return str(err.value)


def test_a_session_over_the_limit_is_refused(conn, home, tmp_path):
    _landed(conn, "s1", n=10)
    assert "rate limit" in _refused(tmp_path)


def test_below_the_limit_the_push_goes_on(conn, home, tmp_path):
    _landed(conn, "s1", n=9)
    # tmp_path is not a repo, so the push stops later and for another reason
    assert "rate limit" not in _refused(tmp_path)


def test_other_sessions_and_other_targets_do_not_count(conn, home, tmp_path):
    _landed(conn, "s2", n=20)
    _landed(conn, "s1", n=20, target="release")
    assert "rate limit" not in _refused(tmp_path)


def test_old_pushes_fall_out_of_the_window(conn, home, tmp_path):
    _landed(conn, "s1", n=20, age=3 * 3600)
    assert "rate limit" not in _refused(tmp_path)


def test_the_limit_is_configurable_and_zero_turns_it_off(conn, home, tmp_path):
    (tmp_path / ".cabildo").mkdir()
    cfg = tmp_path / ".cabildo" / "config.toml"
    _landed(conn, "s1", n=3)
    cfg.write_text("[push]\nmax_per_window = 3\n")
    assert "rate limit" in _refused(tmp_path)
    cfg.write_text("[push]\nmax_per_window = 0\n")
    assert "rate limit" not in _refused(tmp_path)


def test_a_request_with_no_session_is_never_limited(conn, home, tmp_path):
    _landed(conn, "s1", n=50)
    assert actions._push_rate_refusal(None, "main", str(tmp_path)) is None
