"""``[sessions] owner = "host"``: the session host owns each Claude, the app only views it."""

from __future__ import annotations

import os
import time

import pytest
from lines import user
from pilot import drive, settle, wait_until

from cabildo import host, host_client
from cabildo.workspace import attach, launch
from cabildo.workspace.hosting import ClaudePane, Target
from cabildo.workspace.launch import Config


@pytest.fixture
def one(transcript, fake_claude) -> None:
    transcript("s0", user("session s0"), mtime=3_000)


def screen(pane: ClaudePane) -> str:
    return pane.board.capture_text()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def host_gone(session_host: host.Host, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while session_host.records() and time.monotonic() < deadline:
        time.sleep(0.05)
    return not session_host.records()


# --- the config option ---------------------------------------------------------------


def write_config(tmp_path, monkeypatch, text: str) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "config" / "workspace" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)


def test_owner_defaults_to_the_app(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    assert launch.load().owner == "app"


def test_owner_host_is_read_from_the_config(tmp_path, monkeypatch) -> None:
    write_config(tmp_path, monkeypatch, '[sessions]\nowner = "host"\n')
    assert launch.load().owner == "host"


@pytest.mark.parametrize("text", ['[sessions]\nowner = "daemon"\n', "[sessions]\nowners = 1\n"])
def test_a_bad_sessions_table_is_refused(tmp_path, monkeypatch, text) -> None:
    write_config(tmp_path, monkeypatch, text)
    with pytest.raises(launch.ConfigError, match="sessions"):
        launch.load()


# --- new and resumed sessions --------------------------------------------------------


def test_a_new_conversation_runs_in_the_host_not_in_the_app(one, session_host, monkeypatch) -> None:
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        await app.action_new()
        pane = app._shown_pane()
        assert pane is not None
        seen["pane"] = (pane.host_id, pane.host_pid)
        seen["records"] = session_host.records()
        pane.close(end_session=False)  # the app quits; the session must stay
        await settle(app, pilot)

    drive(monkeypatch, scenario, config=Config(owner="host"))
    (record,) = seen["records"]
    assert seen["pane"] == (record.id, record.pid)
    assert os.path.basename(record.command[0]) == "claude"  # the fake one
    assert alive(record.pid)  # the viewer closed; Claude did not


def test_the_viewer_shows_what_the_hosted_claude_prints(one, session_host, monkeypatch) -> None:
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        await app.action_new()
        pane = app._shown_pane()
        assert pane is not None
        pane.board.host.write("typed in the pane\n")  # goes viewer -> host -> Claude -> back
        seen["ok"] = await wait_until(pilot, lambda: "typed in the pane" in screen(pane))

    drive(monkeypatch, scenario, config=Config(owner="host"))
    assert seen["ok"]


def test_closing_the_conversation_ends_the_hosted_claude(one, session_host, monkeypatch) -> None:
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        await app.action_new()
        pane = app._shown_pane()
        assert pane is not None
        seen["pid"] = pane.host_pid
        pane.close()  # `x`: the default ends the session too
        await settle(app, pilot)

    drive(monkeypatch, scenario, config=Config(owner="host"))
    assert host_gone(session_host)


def test_an_app_that_does_not_own_sessions_still_shows_a_hosted_one(
    one, session_host, monkeypatch
) -> None:
    record = host_client.start_session(["cat"], cwd="/", session_id="s0")
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        (row,) = app.rows
        target = app._target(row)
        seen["target"] = target
        seen["count"] = len(session_host.records())

    drive(monkeypatch, scenario)  # owner "app", the default
    assert seen["target"].host_id == record.id
    assert seen["target"].command == host_client.attach_command(record.id)
    assert seen["count"] == 1  # it attached; it did not start another Claude


def test_a_host_that_cannot_start_leaves_the_claude_in_the_app(one, monkeypatch) -> None:
    def refuse(*_a, **_k):
        raise host_client.ClientError("no host")

    monkeypatch.setattr(host_client, "start_session", refuse)
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        await app.action_new()
        pane = app._shown_pane()
        assert pane is not None
        seen["host_id"] = pane.host_id
        seen["notes"] = [n.message for n in app._notifications]

    drive(monkeypatch, scenario, config=Config(owner="host"))
    assert seen["host_id"] == ""
    assert any("Claude runs in this app" in n for n in seen["notes"])


# --- reload: the host takes the Claudes the app owns ---------------------------------


def test_reload_gives_an_app_owned_claude_to_the_host(one, session_host, monkeypatch) -> None:
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        # a Claude the app owns, as one started before the option was switched on
        pane = await app._mount(Target(session_ids=(), session_id="s0", command=("cat",)))
        app._show(pane)
        await wait_until(pilot, lambda: pane.pid is not None)
        seen["pid"] = pane.pid
        app.action_reload()
        await settle(app, pilot)
        seen["handoff"] = os.environ.get(attach.ENV, "")
        seen["records"] = session_host.records()

    drive(monkeypatch, scenario, config=Config(owner="host"))
    os.environ.pop(attach.ENV, None)
    (handed,) = attach.decode(seen["handoff"])
    (record,) = seen["records"]
    assert (handed.fd, handed.host_id, handed.host_pid) == (-1, record.id, seen["pid"])
    assert record.adopted and record.pid == seen["pid"]
    assert alive(seen["pid"])  # not hung up: the host holds its terminal now


def test_a_reload_without_the_option_hands_the_terminal_over_as_before(
    one, session_host, monkeypatch
) -> None:
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        pane = await app._mount(Target(session_ids=(), session_id="s0", command=("cat",)))
        app._show(pane)
        await wait_until(pilot, lambda: pane.pid is not None)
        seen["pid"] = pane.pid
        app.action_reload()
        await settle(app, pilot)
        seen["handoff"] = os.environ.get(attach.ENV, "")

    drive(monkeypatch, scenario)
    handed = attach.decode(seen["handoff"])
    os.environ.pop(attach.ENV, None)
    try:
        assert [(h.fd >= 0, h.host_id) for h in handed] == [(True, "")]
        assert session_host.records() == []
    finally:
        for h in handed:
            attach.discard(h.fd)
            attach.hangup(h.pid)


def test_a_restarted_app_attaches_to_the_session_the_host_took(
    one, session_host, monkeypatch
) -> None:
    record = host_client.start_session(["cat"], cwd="/", session_id="s0")
    monkeypatch.setenv(
        attach.ENV,
        attach.encode(
            [
                attach.Handed(
                    fd=-1,
                    pid=os.getpid(),
                    pane_id="pane-1",
                    session_id="s0",
                    cwd="",
                    group="",
                    title="alpha",
                    rows=24,
                    cols=80,
                    host_id=record.id,
                    host_pid=record.pid,
                )
            ]
        ),
    )
    seen: dict = {}

    async def scenario(app, pilot) -> None:
        pane = app._shown_pane()
        assert pane is not None
        seen["pid"] = pane.pid
        pane.board.host.write("echoed by the hosted child\n")
        seen["ok"] = await wait_until(pilot, lambda: "echoed by the hosted child" in screen(pane))
        seen["text"] = screen(pane)
        seen["exited"] = pane.exited

    drive(monkeypatch, scenario)
    assert seen["ok"], seen
    assert seen["pid"] == record.pid  # the pane reports Claude's pid, not the viewer's
    assert len(session_host.records()) == 1  # attached; started nothing
