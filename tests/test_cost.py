"""Cost telemetry: hook_metrics aggregation and the transcript scanner that
measures how much running cabildo costs (injected context, latency, tool spend).
"""

from __future__ import annotations

import json

from cabildo import store, transcript


# --------------------------------------------------------------------------- #
# hook_metrics (context injected + latency)
# --------------------------------------------------------------------------- #
def test_record_and_summarize_hook_metrics(conn):
    store.record_hook_metric(conn, session_id="s1", event="session_start",
                             injected_chars=300, elapsed_ms=12.0)
    store.record_hook_metric(conn, session_id="s1", event="post_tool_use",
                             injected_chars=0, elapsed_ms=20.0)
    store.record_hook_metric(conn, session_id="s1", event="post_tool_use",
                             injected_chars=150, elapsed_ms=22.0)

    m = store.hook_metrics_summary(conn)
    assert m["total"]["fires"] == 3
    assert m["total"]["chars"] == 450
    assert m["total"]["sessions"] == 1

    by_event = {e["event"]: e for e in m["per_event"]}
    ptu = by_event["post_tool_use"]
    assert ptu["fires"] == 2
    assert ptu["injections"] == 1          # only one of the two injected context
    assert ptu["chars"] == 150
    assert abs(ptu["avg_ms"] - 21.0) < 1e-6


def test_hook_metrics_scoped_by_session(conn):
    store.record_hook_metric(conn, session_id="s1", event="stop",
                             injected_chars=10, elapsed_ms=1.0)
    store.record_hook_metric(conn, session_id="s2", event="stop",
                             injected_chars=99, elapsed_ms=1.0)
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
        fh.write(_line({"message": {"content": [
            {"type": "tool_use", "id": "u1", "name": "mcp__cabildo__inbox",
             "input": {"limit": 20}}]}}))
        fh.write(_line({"message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1",
             "content": "X" * 500}]}}))
        # a non-cabildo tool must be ignored entirely
        fh.write(_line({"message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "ls"}}]}}))
        fh.write(_line({"message": {"content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": "Y" * 999}]}}))

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
        fh.write(_line({"message": {"content": [
            {"type": "tool_use", "id": "u1", "name": "mcp__cabildo__send",
             "input": {"body": "hi"}}]}}))
        fh.write(_line({"message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "Z" * 400}]}}))

    agg = transcript.scan(root=tmp_path / "projects")
    assert agg["calls"] == 1
    assert agg["files"] == 1
    assert agg["output_chars"] == 400
    assert agg["tokens_est"] == (agg["input_chars"] + agg["output_chars"]) // transcript.CHARS_PER_TOKEN


def test_scan_can_scope_to_one_session(tmp_path):
    root = tmp_path / "projects" / "slug"
    root.mkdir(parents=True)
    for sid in ("wanted", "other"):
        with (root / f"{sid}.jsonl").open("w") as fh:
            fh.write(_line({"message": {"content": [
                {"type": "tool_use", "id": "u", "name": "mcp__cabildo__inbox",
                 "input": {}}]}}))
            fh.write(_line({"message": {"content": [
                {"type": "tool_result", "tool_use_id": "u", "content": "Q" * 100}]}}))
    agg = transcript.scan(root=tmp_path / "projects", session_id="wanted")
    assert agg["files"] == 1
    assert agg["output_chars"] == 100


def test_scan_missing_root_is_empty(tmp_path):
    agg = transcript.scan(root=tmp_path / "does-not-exist")
    assert agg["calls"] == 0 and agg["files"] == 0
