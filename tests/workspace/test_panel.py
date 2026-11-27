"""``cabildo panel``: the list and the readings as data, and how a conversation opens."""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
from dataclasses import replace

import pytest
from lines import PROJECT, ai_title, assistant, user

from cabildo import cli, gateway, gateway_usage, host, host_client, jupyter
from cabildo import usage as usage_tokens
from cabildo.workspace import agents, intents, panel, plan, sessions, spend, upstreams
from cabildo.workspace.agents import AgentState

VIEW = panel.View(PROJECT)


def state(session_id: str, **changes) -> AgentState:
    base = AgentState(session_id, session_id[:8], "interactive", "", PROJECT, "idle", "", 4242)
    return replace(base, **changes)


def record(host_id: str = "h1", **changes) -> host.Record:
    base = host.Record(host_id, PROJECT, ("claude",), 4242, 24, 80, time.time() - 120)
    return replace(base, **changes)


def reply(text: str) -> dict:
    found = assistant(text)
    found["message"] |= {
        "id": "msg_1",
        "model": "claude-opus-5-5",
        "usage": {"input_tokens": 300_000, "output_tokens": 900},
    }
    return found


@pytest.fixture
def readings(transcript) -> panel.Readings:
    transcript("old-session", user("an older question"), mtime=time.time() - 7200)
    transcript("new-session", user("q"), reply("a"), ai_title("Fix the login"))
    return panel.Readings(rows=tuple(sessions.build_rows(PROJECT)), live={})


def test_before_the_first_read_the_frame_says_so() -> None:
    frame = panel.frame(VIEW, panel.Readings(), time.time())

    assert frame.reading and frame.items == ()
    assert frame.detail is None and frame.plan is None and frame.intents is None


def test_items_are_the_conversations_of_the_list_newest_first(readings) -> None:
    frame = panel.frame(VIEW, readings, time.time())

    assert [(i.id, i.title, i.age[-1]) for i in frame.items] == [
        ("new-session", "Fix the login", "s"),
        ("old-session", "an older question", "h"),
    ]
    assert (frame.items[0].dir, frame.items[0].branch) == (PROJECT, "main")
    assert frame.folder == PROJECT and frame.detail is None


def test_the_selected_conversation_is_described(readings) -> None:
    frame = panel.frame(replace(VIEW, selected="new-session"), readings, time.time())

    detail = frame.detail
    assert frame.selected == "new-session" and detail is not None
    assert detail.agent and detail.model == "opus 5.5"
    assert detail.context == panel.Context("300k", "1M", 30.0, "")
    assert (detail.tokens_in, detail.tokens_out, detail.tokens_cache) == ("300k", "900", "")


@pytest.mark.parametrize(("used", "level"), [(699_000, ""), (700_000, "amber"), (900_000, "red")])
def test_a_context_window_that_fills_is_amber_then_red(used, level) -> None:
    found = panel._context(sessions.Tokens(context=used, model="claude-opus-5-5"))

    assert found is not None and found.level == level


def test_a_conversation_before_its_first_reply_has_no_context(readings) -> None:
    frame = panel.frame(replace(VIEW, selected="old-session"), readings, time.time())

    assert frame.detail == panel.Detail(agent=frame.detail.agent)


@pytest.mark.parametrize(
    ("changes", "mark"),
    [
        ({"status": "waiting"}, "waiting"),
        ({"status": "busy"}, "working"),
        ({"status": "idle"}, "alive"),
        ({"pid": None}, ""),
    ],
)
def test_the_mark_says_what_claude_does(readings, changes, mark) -> None:
    live = {"new-session": state("new-session", **changes)}

    frame = panel.frame(VIEW, replace(readings, live=live), time.time())

    assert frame.items[0].mark == mark


def test_a_hosted_conversation_names_its_session_of_the_host(readings) -> None:
    hosted = (record(session_id="new-session"),)

    frame = panel.frame(VIEW, replace(readings, hosted=hosted), time.time())

    assert [(i.id, i.host_id) for i in frame.items] == [("new-session", "h1"), ("old-session", "")]


def test_a_hosted_session_without_a_transcript_is_listed_first(readings) -> None:
    hosted = (record(), record("far", cwd="/elsewhere", pid=7))

    frame = panel.frame(replace(VIEW, selected="host:h1"), replace(readings, hosted=hosted), 0)

    assert [i.id for i in frame.items] == ["host:h1", "new-session", "old-session"]
    assert frame.items[0].title == "new conversation"
    assert frame.selected == "host:h1" and frame.detail is not None
    everywhere = panel.frame(replace(VIEW, scope="all"), replace(readings, hosted=hosted), 0)
    assert "host:far" in [i.id for i in everywhere.items]


def test_a_conversation_on_the_bus_can_be_messaged(readings) -> None:
    frame = panel.frame(VIEW, replace(readings, bus=frozenset({"old-session"})), time.time())

    assert [i.bus_id for i in frame.items] == ["", "old-session"]


def test_a_long_list_keeps_the_running_ones(readings, monkeypatch) -> None:
    monkeypatch.setattr(panel, "MAX_ITEMS", 1)
    live = {"old-session": state("old-session")}

    frame = panel.frame(VIEW, replace(readings, live=live), time.time())

    assert [i.id for i in frame.items] == ["old-session"]
    assert frame.older == 1


# --- the machine-wide readings -------------------------------------------------


def test_a_plan_limit_says_how_far_its_window_is_and_when_it_fills(readings) -> None:
    now = 1_800_000_000.0
    usage = plan.PlanUsage("max", plan.Limit(72.0, now + 3600), plan.Limit(100.0, now + 86400))
    pace = plan.PlanPace(
        plan.Pace(40.0, 72.0, now + 3600, now, ready=True),
        plan.Pace(1.0, 100.0, now + 86400, now, ready=True),
    )

    found = panel.frame(VIEW, replace(readings, plan=plan.Shared(usage, pace, now)), now).plan

    assert found is not None and found.name == "max" and not found.elsewhere
    five, seven = found.limits
    assert (five.name, five.percent, five.level, five.elapsed) == ("5h", 72.0, "amber", 80.0)
    assert five.pace == "40.0%/h"
    assert five.full == time.strftime("%H:%M", time.localtime(now + 0.7 * 3600))
    assert five.resets == time.strftime("%H:%M", time.localtime(now + 3600))
    # nothing is left of the 7-day limit: no pace, no time it fills
    assert (seven.level, seven.pace, seven.full) == ("red", "", "")
    assert seven.resets == time.strftime("%a %H:%M", time.localtime(now + 86400))


def status(name: str, **changes) -> gateway.UpstreamStatus:
    return replace(gateway.UpstreamStatus(name, "bedrock", True, 0, "", 7, 1, "", False), **changes)


def test_the_gateway_names_the_upstream_of_the_selected_conversation(readings) -> None:
    week = usage_tokens.Window(input=1000, cost=12.4)
    view = upstreams.View(
        (
            status("anthropic", kind="passthrough", ready=False, set_aside_s=180, reason="limit"),
            status("bedrock-eu"),
        ),
        (upstreams.SessionUpstream("new-session", "bedrock-eu"),),
        (gateway_usage.Spent("bedrock-eu", usage_tokens.Window(cost=3.2), week, week),),
    )
    shared = plan.Shared(plan.PlanUsage("max", None, None), plan.PlanPace(None, None), 0)
    found = replace(readings, upstreams=view, plan=shared)

    frame = panel.frame(replace(VIEW, selected="new-session"), found, time.time())

    first, second = frame.gateway
    assert (first.ready, first.set_aside, first.reason, first.today) == (False, "3m", "limit", "")
    assert (second.serving, second.requests, second.failovers) == (True, 7, 1)
    assert (second.today, second.week) == ("$3.20", "$12.40")
    assert frame.detail is not None and frame.detail.upstream == "bedrock-eu"
    assert frame.plan is not None and frame.plan.elsewhere  # the limits do not stop this one


def test_the_spend_is_a_column_per_period_and_a_row_per_model(readings) -> None:
    def window(cost: float) -> usage_tokens.Window:
        return usage_tokens.Window(cost=cost)

    opus = usage_tokens.ModelSpend("claude-opus-5-5", 2_000_000, 30.0, priced=True)
    haiku = usage_tokens.ModelSpend("claude-haiku-5-5", 500_000, 10.0, priced=True)
    report = spend.Report(
        usage_tokens.Spend(window(1.5), window(40.0), window(900.0)),
        billed_week=38.0,
        session=usage_tokens.Spend(window(0.0), window(0.0), window(0.32)),
        models=(opus, haiku),
        split={"7d": {opus.model: opus, haiku.model: haiku}, "all": {opus.model: opus}},
    )
    ids = readings.rows[0].ids
    found = replace(readings, spend=report, spend_label="spend", spend_ids=ids)

    frame = panel.frame(replace(VIEW, selected="new-session"), found, time.time())

    assert frame.spend is not None and frame.spend.billed
    assert [(p.name, p.total, p.billed) for p in frame.spend.periods] == [
        ("today", "$1.50", "-"),
        ("7d", "$40.00", "$38.00"),
        ("all", "$900.00", "-"),
        ("session", "$0.32", ""),
    ]
    assert frame.spend.models == (
        panel.Model("opus 5.5", ("-", "$30.00", "$30.00", "-"), 75.0, "2.0M"),
        panel.Model("haiku 5.5", ("-", "$10.00", "-", "-"), 25.0, "500k"),
    )
    assert frame.detail is not None and frame.detail.cost == "$0.32"
    # the report is still about another conversation: its session column is not shown
    other = panel.frame(replace(VIEW, selected="old-session"), found, time.time())
    assert [p.name for p in other.spend.periods] == ["today", "7d", "all"]
    assert other.detail is not None and other.detail.cost == ""


def test_intents_say_which_are_queued_and_why_one_failed(readings) -> None:
    def intent(ident: int, status: str, reason: str = "") -> intents.Intent:
        return intents.Intent(ident, "push", status, "cabildo", "", "", "robot", 940.0, reason)

    queued = (intent(1, "running"), intent(2, "failed", "dirty tree"), intent(3, "done", "x"))

    frame = panel.frame(VIEW, replace(readings, intents=queued), 1000.0)

    assert frame.intents is not None
    assert [(i.kind, i.age, i.reason) for i in frame.intents] == [
        ("queue", "1m", ""),
        ("failed", "1m", "dirty tree"),
        ("done", "1m", ""),
    ]
    assert panel.frame(VIEW, replace(readings, intents=()), 0).intents == ()


def test_only_running_runners_are_listed(readings) -> None:
    runners = (
        jupyter.RunnerRow("lab", running=True, paused=True, pid=1, port=1, cwd="/"),
        jupyter.RunnerRow("off", running=False, paused=False, pid=None, port=None, cwd="/"),
    )

    frame = panel.frame(VIEW, replace(readings, runners=runners), time.time())

    assert frame.runners == (panel.Runner("lab", paused=True, cpu="…", mem="…"),)


# --- open --------------------------------------------------------------------


@pytest.fixture
def started(monkeypatch) -> list[dict]:
    """No host, no ``claude agents``: record what would be started."""
    calls: list[dict] = []

    def start(command, **keywords) -> host.Record:
        calls.append({"command": tuple(command), **keywords})
        return record("fresh")

    monkeypatch.setattr(host_client, "start_session", start)
    monkeypatch.setattr(host_client, "list_sessions", list)
    monkeypatch.setattr(agents, "shared_live_agents", lambda _at: agents.Snapshot({}, 0))
    # the directory of the transcripts counts as local, so no directory has to exist
    return calls


def live_now(monkeypatch, found: dict) -> None:
    monkeypatch.setattr(agents, "shared_live_agents", lambda _at: agents.Snapshot(found, 0))


def test_opening_a_stopped_conversation_resumes_it_in_the_host(readings, started) -> None:
    opened = panel.open_conversation(VIEW, readings, "new-session", False)

    assert (opened.host_id, opened.error, opened.title) == ("fresh", "", "Fix the login")
    assert started == [
        {
            "command": ("claude", "--resume", "new-session"),
            "cwd": PROJECT,
            "group": "",
            "session_id": "new-session",
        }
    ]


def test_opening_a_hosted_conversation_attaches(readings, started, monkeypatch) -> None:
    owned = [record(session_id="new-session")]
    monkeypatch.setattr(host_client, "list_sessions", lambda: owned)

    opened = panel.open_conversation(VIEW, readings, "new-session", False)

    assert opened.host_id == "h1" and started == []
    assert panel.open_conversation(VIEW, readings, "host:h9", False).host_id == "h9"


def test_a_background_conversation_is_attached_through_claude(
    readings, started, monkeypatch
) -> None:
    live_now(monkeypatch, {"new-session": state("new-session", kind="background")})

    opened = panel.open_conversation(VIEW, readings, "new-session", False)

    assert opened.command == ("claude", "attach", "new-sess")
    assert opened.cwd == PROJECT and started == []


def test_a_conversation_of_another_terminal_needs_force(readings, started, monkeypatch) -> None:
    live_now(monkeypatch, {"new-session": state("new-session")})

    asked = panel.open_conversation(VIEW, readings, "new-session", False)

    assert asked.elsewhere and started == []
    assert panel.open_conversation(VIEW, readings, "new-session", True).host_id == "fresh"


def test_opening_an_unknown_conversation_says_so(readings, started) -> None:
    assert "not in the list" in panel.open_conversation(VIEW, readings, "gone", False).error


# --- the process -------------------------------------------------------------


class Lines(io.TextIOBase):
    """A stdout that hands each written JSON line to the test."""

    def __init__(self) -> None:
        self.found: queue.Queue[dict] = queue.Queue()

    def write(self, text: str) -> int:
        self.found.put(json.loads(text))
        return len(text)

    def until(self, check) -> dict:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            message = self.found.get(timeout=10)
            if check(message):
                return message
        raise AssertionError("no such line")


def test_the_server_answers_commands_and_ends_with_stdin(readings) -> None:
    read_scopes: list[str] = []

    def rows(view: panel.View, _readings: panel.Readings) -> dict[str, object]:
        read_scopes.append(view.scope)
        return {"rows": readings.rows}

    def broken(_view, _readings):
        raise RuntimeError("no gateway")

    out = Lines()
    server = panel.Server(
        VIEW, out, (panel.Source("rows", 60, rows), panel.Source("broken", 60, broken))
    )
    commands: queue.Queue[str | None] = queue.Queue()
    thread = threading.Thread(target=server.serve, args=(iter(commands.get, None),))
    thread.start()
    try:
        out.until(lambda m: len(m["items"]) == 2)
        commands.put("not json")
        commands.put(json.dumps({"cmd": "select", "id": "old-session"}))
        out.until(lambda m: m["selected"] == "old-session")
        commands.put(json.dumps({"cmd": "scope", "scope": "all"}))
        out.until(lambda m: m["scope"] == "all" and not m["reading"])
        commands.put(json.dumps({"cmd": "open", "id": "host:abc"}))
        assert out.until(lambda m: m["type"] == "open")["host_id"] == "abc"
    finally:
        commands.put(None)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert read_scopes[-1] == "all"


def test_once_prints_one_frame_of_the_directory(transcript, monkeypatch, capsys) -> None:
    transcript("only", user("hello"))
    monkeypatch.setattr(agents, "shared_live_agents", lambda _at: agents.Snapshot({}, 0))
    monkeypatch.setattr(host_client, "list_sessions", list)
    monkeypatch.chdir(os.path.dirname(__file__))

    assert cli.main(["panel", "--once", "--cwd", PROJECT]) == 0

    frame = json.loads(capsys.readouterr().out)
    assert [i["title"] for i in frame["items"]] == ["hello"]
    assert frame["type"] == "frame" and not frame["reading"]
