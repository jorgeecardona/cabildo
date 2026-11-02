import json
import subprocess
import time

from cabildo.workspace import plan, xdgdirs
from cabildo.workspace.app import fmt_plan, plan_style
from cabildo.workspace.plan import Limit, PlanUsage

REPLY = {
    "five_hour": {"utilization": 18.0, "resets_at": "2026-10-25T20:10:00.041540+00:00"},
    "seven_day": {"utilization": 35.0, "resets_at": "2026-10-31T03:00:00+00:00"},
    "seven_day_opus": None,
}


def test_parse_reads_both_limits():
    got = plan.parse("max", REPLY)
    assert got and got.plan == "max"
    assert got.five_hour and got.five_hour.percent == 18.0
    assert got.seven_day and got.seven_day.resets_at == 1793415600.0


def test_parse_without_limits_is_none():
    assert plan.parse("max", {"five_hour": None}) is None


def test_no_credentials_no_fetch():
    assert plan.fetch() is None  # conftest: an empty config dir


def test_expired_token_is_not_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    oauth = {"accessToken": "t", "subscriptionType": "max", "expiresAt": 1000}
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    assert plan.fetch() is None


def test_fmt_plan_columns_line_up_with_the_grid():
    reset = time.mktime((2026, 10, 25, 21, 10, 0, 0, 0, -1))
    lines = fmt_plan(PlanUsage("max", Limit(18, reset), Limit(35, 0)))
    assert lines == [
        "max plan         used      pace  resets  ETA",
        "5h                18%            21:10",
        "7d                35%",
    ]


def test_plan_style_follows_the_worst_limit():
    assert plan_style(PlanUsage("max", Limit(10, 0), Limit(75, 0))) == "bold dark_orange3"
    assert plan_style(PlanUsage("pro", Limit(95, 0), None)) == "bold red"
    assert plan_style(PlanUsage("pro", Limit(5, 0), None)) == ""


def test_bad_reset_time_keeps_the_percent():
    got = plan.parse("max", {"five_hour": {"utilization": 40, "resets_at": "soon"}})
    assert got and got.five_hour == Limit(40.0, 0)


def test_a_cut_reply_is_none_not_a_crash(tmp_path, monkeypatch):
    import http.client

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    oauth = {"accessToken": "t", "subscriptionType": "max", "expiresAt": 0}
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))

    def cut(*_a, **_k):
        raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(plan.urllib.request, "urlopen", cut)
    assert plan.fetch() is None


def test_mode_line_grows_to_fit_the_plan_row(monkeypatch):
    from pilot import drive

    seen: list = []

    async def scenario(app, pilot) -> None:
        usage = PlanUsage("max", Limit(18, 0), Limit(35, 0))
        app._plan_got(plan.Shared(usage, plan.PlanPace(None, None), 0.0))
        await pilot.pause()
        box = app.query_one("#modeline")
        lines = str(box.render()).split("\n")
        seen.append((box.size.height, len(lines), any("max plan" in x for x in lines)))

    drive(monkeypatch, scenario)
    height, n, shown = seen[0]
    assert shown and height == n


def a_pace(
    rate: float, percent: float, resets_at: float, at: float, ready: bool = True
) -> plan.Pace:
    return plan.Pace(rate, percent, resets_at, at, ready)


def feed(percents, start: float, step: float, reset: float, prev=None, *, seven_day=False):
    """The history after one sample per ``step`` seconds from ``start``: of the 5-hour
    limit, or of the 7-day limit with ``seven_day``.
    """
    for i, pct in enumerate(percents):
        lim = Limit(pct, reset)
        usage = PlanUsage("max", None, lim) if seven_day else PlanUsage("max", lim, None)
        prev = plan.record(prev, usage, start + i * step)
    return prev


def test_a_steady_fill_reads_its_rate():
    h = plan.HOUR
    hist = feed([10 + int(i / 6) for i in range(61)], 6 * h, 60, 10 * h)  # 10%/h, rounded
    p = plan.pace(hist, 7 * h).five_hour
    assert p and p.ready and abs(p.rate - 10.0) < 1.0


def test_exact_samples_at_uneven_times_give_the_exact_rate():
    times = [0, 60, 70, 80, 90, 1000, 1100, 3000]
    samples = tuple(plan.Sample(t, 5 + t / 360) for t in times)  # 10%/h
    r = plan.rate(samples, now=3000, half_life=1800)
    assert r is not None and abs(r - 10.0) < 1e-9


def test_the_seven_day_pace_over_a_day_reads_like_an_average():
    h = plan.HOUR
    busy = feed([10 + int(i / 6) for i in range(61)], 6 * h, 60, 7 * 24 * h, seven_day=True)
    idle = feed([20] * 60, 7 * h + 60, 60, 7 * 24 * h, busy, seven_day=True)
    p = plan.pace(idle, 8 * h).seven_day
    # 10% in the busy hour, none in the idle hour: about 5%/h, a little under as the idle
    # hour is newer
    assert p and p.ready and 4.5 < p.rate < 5.0


def test_an_idle_stretch_brings_the_pace_down():
    h = plan.HOUR
    busy = feed([10 + int(i / 6) for i in range(61)], 6 * h, 60, 10 * h)  # 10%/h for 1 h
    idle = feed([20] * 60, 7 * h + 60, 60, 10 * h, busy)  # then nothing for 1 h
    before = plan.pace(busy, 7 * h).five_hour
    after = plan.pace(idle, 8 * h).five_hour
    # the busy hour is 1 to 2 half-lives old: weight (1/4 - 1/16) / (1 - 1/16) = 1/5
    assert before and after and abs(after.rate - before.rate / 5) < 0.5


def test_a_new_window_restarts_the_history():
    h = plan.HOUR
    old = feed([80, 85, 90], 8 * h, 600, 10 * h)
    new = feed([5], 11 * h, 60, 15 * h, old)
    assert new.five_hour and len(new.five_hour.samples) == 1


def test_old_samples_are_dropped():
    h = plan.HOUR
    hist = feed([10] * 200, 0, 60, 10 * h)
    assert hist.five_hour
    assert hist.five_hour.samples[0].at >= 199 * 60 - plan.KEEP["five_hour"]


def test_full_at_only_before_the_reset():
    h = plan.HOUR
    assert a_pace(10.0, 80, 11 * h, 8 * h).full_at() == 10 * h
    assert a_pace(10.0, 80, 9 * h, 8 * h).full_at() is None
    assert a_pace(0.0, 80, 9 * h, 8 * h).full_at() is None


def test_fmt_plan_shows_the_pace_and_when_a_limit_fills():
    now = time.mktime((2026, 10, 25, 18, 0, 0, 0, 0, -1))  # a Sunday
    h = plan.HOUR
    usage = PlanUsage("max", Limit(80, now + 3 * h), Limit(50, now + 48 * h))
    pace = plan.PlanPace(a_pace(10.0, 80, now + 3 * h, now), a_pace(0.4, 50, now + 48 * h, now))
    _, five, seven = fmt_plan(usage, pace)
    assert five.split() == ["5h", "80%", "10.0%/h", "21:00", "20:00"]
    assert seven.split() == ["7d", "50%", "0.4%/h", "Tue"]  # resets before it fills


def _fetching(monkeypatch, *percents):
    """``plan.fetch`` returns these 5-hour percents in turn (None: a failed ask)."""
    calls = []
    reset = time.time() + 3 * plan.HOUR
    answers = iter(percents)

    def fetch():
        calls.append(1)
        pct = next(answers)
        return None if pct is None else PlanUsage("max", Limit(pct, reset), None)

    monkeypatch.setattr(plan, "fetch", fetch)
    return calls


def test_instances_share_one_fetch(monkeypatch):
    calls = _fetching(monkeypatch, 20.0)
    first = plan.shared(not_before=0)
    second = plan.shared(not_before=time.time() - 300)  # another instance, a moment later
    assert len(calls) == 1 and first == second
    assert first and first.usage.five_hour and first.usage.five_hour.percent == 20.0


def test_a_new_fetch_moves_the_shared_pace(monkeypatch):
    _fetching(monkeypatch, 20.0, 30.0)
    first = plan.shared(not_before=0)
    second = plan.shared(not_before=time.time() + 1)  # too old: fetched again
    assert first and second and first.pace.five_hour and second.pace.five_hour
    assert second.pace.five_hour.at > first.pace.five_hour.at
    assert second.pace.five_hour.percent == 30.0  # built on the shared sample, not restarted


def test_a_failed_fetch_keeps_a_recent_sample_and_drops_an_old_one(monkeypatch):
    _fetching(monkeypatch, 20.0, None, None)
    first = plan.shared(not_before=0)
    assert plan.shared(not_before=time.time() + 1) == first  # failed: last numbers
    (path,) = (xdgdirs.runtime_home() / xdgdirs.APP).glob("usage-*.json")
    raw = json.loads(path.read_text())
    raw["taken_at"] -= 2 * plan.STALE
    path.write_text(json.dumps(raw))
    assert plan.shared(not_before=time.time() + 1) is None


def test_no_credentials_share_nothing():
    assert plan.shared(not_before=0) is None  # conftest: an empty config dir


def test_another_config_dir_is_another_account(monkeypatch, tmp_path):
    calls = _fetching(monkeypatch, 20.0, 70.0)
    mine = plan.shared(not_before=0)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work"))
    other = plan.shared(not_before=time.time() - 300)  # fresh for the first account
    assert len(calls) == 2 and mine != other
    assert other and other.usage.five_hour and other.usage.five_hour.percent == 70.0


def test_no_pace_until_the_samples_span_the_baseline():
    h = plan.HOUR
    young = feed([1, 2], 5 * h, 100, 10 * h)  # 1% in 100 s would read 36%/h
    p = plan.pace(young, 5 * h + 100).five_hour
    assert p and not p.ready and p.full_at() is None
    ok = feed([1] * 16, 5 * h, 60, 10 * h)
    q = plan.pace(ok, 5.25 * h).five_hour
    assert q and q.ready


def test_fmt_plan_shows_a_dash_until_the_pace_is_ready():
    now = time.mktime((2026, 10, 25, 18, 0, 0, 0, 0, -1))
    h = plan.HOUR
    usage = PlanUsage("max", Limit(2, now + 4.9 * h), None)
    unready = plan.PlanPace(a_pace(36.0, 2, now + 4.9 * h, now, ready=False), None)
    _, five = fmt_plan(usage, unready)
    assert five.split() == [
        "5h",
        "2%",
        "-",
        time.strftime("%H:%M", time.localtime(now + 4.9 * h)),
    ]


def test_after_a_failed_fetch_nobody_fetches_until_the_wait_has_passed(monkeypatch):
    calls = _fetching(monkeypatch, None, None, 20.0)
    assert plan.shared(not_before=time.time() + 1) is None  # fails: a wait starts
    assert plan.shared(not_before=time.time() + 1) is None  # another instance: no request
    assert len(calls) == 1
    retry = next((xdgdirs.runtime_home() / xdgdirs.APP).glob("plan-*.retry"))
    raw = json.loads(retry.read_text())
    assert raw["step"] == plan.BACKOFF_FIRST
    retry.write_text(json.dumps({"until": 0, "step": raw["step"]}))  # the wait is over
    assert plan.shared(not_before=time.time() + 1) is None  # fails again: twice as long
    assert len(calls) == 2
    assert json.loads(retry.read_text())["step"] == 2 * plan.BACKOFF_FIRST
    retry.write_text(json.dumps({"until": 0, "step": 2 * plan.BACKOFF_FIRST}))
    got = plan.shared(not_before=time.time() + 1)  # works: the wait is forgotten
    assert got and not retry.exists()


def _stored_file():
    (path,) = (xdgdirs.runtime_home() / xdgdirs.APP).glob("usage-*.json")
    return path


def test_the_shared_file_carries_its_format_and_extra_keys_are_ignored(monkeypatch):
    _fetching(monkeypatch, 20.0)
    first = plan.shared(not_before=0)
    path = _stored_file()
    raw = json.loads(path.read_text())
    assert raw["format"] == plan.FORMAT
    raw["usage"]["five_hour"]["new_key"] = 1  # a later version adds keys
    raw["something_new"] = {"x": 1}
    path.write_text(json.dumps(raw))
    assert plan.shared(not_before=0) == first


def test_a_newer_format_is_shown_but_never_fetched_over(monkeypatch):
    calls = _fetching(monkeypatch, 20.0, 30.0)
    plan.shared(not_before=0)
    path = _stored_file()
    raw = json.loads(path.read_text())
    raw["format"] = plan.FORMAT + 1
    path.write_text(json.dumps(raw))
    got = plan.shared(not_before=time.time() + 1)  # stale, but a newer workspace owns it
    assert len(calls) == 1 and got and got.usage.five_hour
    assert got.usage.five_hour.percent == 20.0
    assert json.loads(path.read_text())["format"] == plan.FORMAT + 1


def test_two_reads_of_the_same_sample_show_the_same_pace(monkeypatch):
    _fetching(monkeypatch, 20.0, 21.0)
    plan.shared(not_before=0)
    plan.shared(not_before=time.time() + 1)
    first = plan.shared(not_before=0)
    monkeypatch.setattr(time, "time", lambda: first.taken_at + 40)  # read 40 s later
    assert first == plan.shared(not_before=0)


def test_macos_reads_the_token_from_the_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))  # no .credentials.json here
    monkeypatch.setenv("USER", "jorge")
    monkeypatch.setattr(plan.sys, "platform", "darwin")
    oauth = {"accessToken": "kc", "subscriptionType": "max", "expiresAt": 0}
    calls = []

    def run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"claudeAiOauth": oauth}) + "\n")

    monkeypatch.setattr(plan.subprocess, "run", run)
    assert plan._token() == ("max", "kc")
    assert calls == [
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            "jorge",
            "-s",
            "Claude Code-credentials",
            "-w",
        ]
    ]


def test_linux_never_asks_the_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(plan.sys, "platform", "linux")
    monkeypatch.setattr(
        plan.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert plan._token() is None


def test_keychain_miss_is_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(plan.sys, "platform", "darwin")

    def run(cmd, **_kw):
        raise subprocess.CalledProcessError(44, cmd)  # security: item not found

    monkeypatch.setattr(plan.subprocess, "run", run)
    assert plan._token() is None
