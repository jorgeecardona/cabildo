"""Cost telemetry: hook_metrics aggregation and the transcript scanner that
measures how much running cabildo costs (injected context, latency, tool spend).
"""

from __future__ import annotations

import json
import os

from cabildo import store, transcript


# --------------------------------------------------------------------------- #
# hook_metrics (context injected + latency)
# --------------------------------------------------------------------------- #
def test_record_and_summarize_hook_metrics(conn):
    store.record_hook_metric(
        conn, session_id="s1", event="session_start", injected_chars=300, elapsed_ms=12.0
    )
    store.record_hook_metric(
        conn, session_id="s1", event="post_tool_use", injected_chars=0, elapsed_ms=20.0
    )
    store.record_hook_metric(
        conn, session_id="s1", event="post_tool_use", injected_chars=150, elapsed_ms=22.0
    )

    m = store.hook_metrics_summary(conn)
    assert m["total"]["fires"] == 3
    assert m["total"]["chars"] == 450
    assert m["total"]["sessions"] == 1

    by_event = {e["event"]: e for e in m["per_event"]}
    ptu = by_event["post_tool_use"]
    assert ptu["fires"] == 2
    assert ptu["injections"] == 1  # only one of the two injected context
    assert ptu["chars"] == 150
    assert abs(ptu["avg_ms"] - 21.0) < 1e-6


def test_hook_metrics_scoped_by_session(conn):
    store.record_hook_metric(conn, session_id="s1", event="stop", injected_chars=10, elapsed_ms=1.0)
    store.record_hook_metric(conn, session_id="s2", event="stop", injected_chars=99, elapsed_ms=1.0)
    m = store.hook_metrics_summary(conn, session_id="s2")
    assert m["total"]["fires"] == 1
    assert m["total"]["chars"] == 99


# --------------------------------------------------------------------------- #
# transcript scanner (agent tool traffic)
# --------------------------------------------------------------------------- #
def _line(obj) -> str:
    return json.dumps(obj) + "\n"


def test_scan_file_counts_cabildo_calls_and_results(tmp_path):
    f = tmp_path / "sess.jsonl"
    with f.open("w") as fh:
        # cabildo inbox call + its result
        fh.write(
            _line(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "u1",
                                "name": "mcp__cabildo__inbox",
                                "input": {"limit": 20},
                            }
                        ]
                    }
                }
            )
        )
        fh.write(
            _line(
                {
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "u1", "content": "X" * 500}
                        ]
                    }
                }
            )
        )
        # a non-cabildo tool must be ignored entirely
        fh.write(
            _line(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "b1",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            }
                        ]
                    }
                }
            )
        )
        fh.write(
            _line(
                {
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "b1", "content": "Y" * 999}
                        ]
                    }
                }
            )
        )

    by_tool = transcript.scan_file(f)
    assert set(by_tool) == {"mcp__cabildo__inbox"}
    e = by_tool["mcp__cabildo__inbox"]
    assert e["calls"] == 1
    assert e["output_chars"] == 500
    assert e["input_chars"] == len(json.dumps({"limit": 20}))


def test_scan_aggregates_and_estimates_tokens(tmp_path):
    root = tmp_path / "projects" / "slug"
    root.mkdir(parents=True)
    f = root / "abc123.jsonl"
    with f.open("w") as fh:
        fh.write(
            _line(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "u1",
                                "name": "mcp__cabildo__send",
                                "input": {"body": "hi"},
                            }
                        ]
                    }
                }
            )
        )
        fh.write(
            _line(
                {
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "u1", "content": "Z" * 400}
                        ]
                    }
                }
            )
        )

    agg = transcript.scan(root=tmp_path / "projects")
    assert agg["calls"] == 1
    assert agg["files"] == 1
    assert agg["output_chars"] == 400
    assert (
        agg["tokens_est"]
        == (agg["input_chars"] + agg["output_chars"]) // transcript.CHARS_PER_TOKEN
    )


def test_scan_can_scope_to_one_session(tmp_path):
    root = tmp_path / "projects" / "slug"
    root.mkdir(parents=True)
    for sid in ("wanted", "other"):
        with (root / f"{sid}.jsonl").open("w") as fh:
            fh.write(
                _line(
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "u",
                                    "name": "mcp__cabildo__inbox",
                                    "input": {},
                                }
                            ]
                        }
                    }
                )
            )
            fh.write(
                _line(
                    {
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": "u", "content": "Q" * 100}
                            ]
                        }
                    }
                )
            )
    agg = transcript.scan(root=tmp_path / "projects", session_id="wanted")
    assert agg["files"] == 1
    assert agg["output_chars"] == 100


def test_scan_missing_root_is_empty(tmp_path):
    agg = transcript.scan(root=tmp_path / "does-not-exist")
    assert agg["calls"] == 0 and agg["files"] == 0


# --------------------------------------------------------------------------- #
# session digests (recover a lost conversation among many open tabs)
# --------------------------------------------------------------------------- #
def _user(obj_text):
    # Claude Code writes compact JSON; the digest's cheap gate looks for the
    # exact substring `"type":"user"`, so emit compact here too.
    if isinstance(obj_text, str):
        obj = {"type": "user", "message": {"content": obj_text}}
    else:
        obj = obj_text
    return json.dumps(obj, separators=(",", ":")) + "\n"


def test_project_slug_matches_claude_code_layout():
    assert transcript.project_slug("/home/jo/my.app") == "-home-jo-my-app"
    assert transcript.project_slug("/a/b-c_d") == "-a-b-c-d"


def test_session_digest_first_last_and_turn_count(tmp_path):
    f = tmp_path / "sess.jsonl"
    with f.open("w") as fh:
        fh.write(_user("first thing I asked"))
        fh.write(_user("a middle turn"))
        fh.write(_user("where I left off"))
    dig = transcript.session_digest(f)
    assert dig is not None
    assert dig["session_id"] == "sess"
    assert dig["title"] == "first thing I asked"
    assert dig["last"] == "where I left off"
    assert dig["turns"] == 3


def test_session_digest_ignores_noise_lines(tmp_path):
    f = tmp_path / "noisy.jsonl"
    with f.open("w") as fh:
        # tool_result rides on a user-type line — not a human turn
        fh.write(
            _user(
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "out"}]
                    },
                }
            )
        )
        # meta / hook / wrapper injections
        fh.write(_user({"type": "user", "isMeta": True, "message": {"content": "meta stuff"}}))
        fh.write(_user("<command-name>/clear</command-name>"))
        fh.write(_user("[Request interrupted by user]"))
        fh.write(_user("Caveat: the messages below were generated"))
        # the one genuine turn
        fh.write(_user("the real question"))
    dig = transcript.session_digest(f)
    assert dig is not None
    assert dig["turns"] == 1
    assert dig["title"] == "the real question"
    assert dig["last"] == "the real question"


def test_session_digest_none_when_no_user_turn(tmp_path):
    f = tmp_path / "subagent.jsonl"
    with f.open("w") as fh:
        fh.write(_line({"type": "assistant", "message": {"content": "hi"}}))
    assert transcript.session_digest(f) is None


def test_list_sessions_scopes_by_slug_and_sorts_newest_first(tmp_path):
    root = tmp_path / "projects"
    mine = root / "-me-proj"
    other = root / "-other-proj"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)
    (mine / "old.jsonl").write_text(_user("older session"))
    (mine / "new.jsonl").write_text(_user("newer session"))
    (other / "elsewhere.jsonl").write_text(_user("different project"))
    # make "new" strictly newer than "old"
    os.utime(mine / "old.jsonl", (1000, 1000))
    os.utime(mine / "new.jsonl", (2000, 2000))

    scoped = transcript.list_sessions(root=root, slug="-me-proj")
    assert [d["session_id"] for d in scoped] == ["new", "old"]
    assert all(d["project_dir"] == "-me-proj" for d in scoped)

    everywhere = {d["session_id"] for d in transcript.list_sessions(root=root)}
    assert everywhere == {"new", "old", "elsewhere"}


def test_list_sessions_skips_subagent_subdirs(tmp_path):
    root = tmp_path / "projects"
    proj = root / "-me-proj"
    side = proj / "sidechain"
    side.mkdir(parents=True)
    (proj / "main.jsonl").write_text(_user("top-level session"))
    (side / "sub.jsonl").write_text(_user("sub-agent log"))
    ids = {d["session_id"] for d in transcript.list_sessions(root=root, slug="-me-proj")}
    assert ids == {"main"}
