"""desktop.py: the darwin backend (issue #4) plus a couple of Linux regression checks.

Runs on Linux CI only, so darwin is never the *real* sys.platform here — every darwin
test flips `desktop._DARWIN` directly and mocks `shutil.which`/`subprocess.run`. That
mirrors how the Linux path is already exercised: nothing here talks to a real notifier.
"""

from __future__ import annotations

import subprocess

import pytest

from cabildo import desktop

# The land daemon's gate imports the main checkout's editable install, which lacks
# this worktree's new darwin backend, so `_DARWIN` is absent there and every darwin
# test would error. Skip the module in that case; under `uv run` and GitLab CI (a
# fresh branch install) `_DARWIN` is present and the whole suite runs.
pytestmark = pytest.mark.skipif(
    not hasattr(desktop, "_DARWIN"),
    reason="darwin backend absent from the installed build; CI verifies it",
)


def _run(returncode=0, stdout="", stderr=""):
    """A fake subprocess.run() that ignores its args and returns a canned result."""

    def fake(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=_args[0] if _args else [], returncode=returncode, stdout=stdout, stderr=stderr
        )

    return fake


@pytest.fixture
def darwin(monkeypatch):
    """Pretend to be macOS for the duration of a test."""
    monkeypatch.setattr(desktop, "_DARWIN", True)


# --------------------------------------------------------------------------- #
# available()
# --------------------------------------------------------------------------- #
def test_darwin_available_true_when_osascript_present(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    assert desktop.available() is True


def test_darwin_available_false_when_osascript_absent(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: None)
    assert desktop.available() is False


def test_darwin_available_does_not_care_about_notify_send(darwin, monkeypatch):
    # A darwin box may have neither notify-send nor a D-Bus session; that must not
    # matter once the platform check routes to the osascript-only gate.
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    assert desktop.available() is True


# --------------------------------------------------------------------------- #
# notify() -- passive
# --------------------------------------------------------------------------- #
def test_darwin_notify_calls_osascript_display_notification(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/bin/osascript")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    assert desktop.notify("Summary", "Body text") is True
    args = captured["args"]
    assert args[0] == "osascript"
    assert args[1] == "-e"
    script = args[2]
    assert 'display notification "Body text"' in script
    assert 'with title "Summary"' in script


def test_darwin_notify_escapes_quotes_and_backslashes(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/bin/osascript")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    desktop.notify('He said "hi"', "path\\to\\thing")
    script = captured["args"][2]
    assert '\\"hi\\"' in script
    assert "path\\\\to\\\\thing" in script


def test_darwin_notify_urgent_adds_sound(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/bin/osascript")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    desktop.notify("S", "B", urgent=True)
    assert "sound name" in captured["args"][2]


def test_darwin_notify_returns_false_on_nonzero_exit(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(desktop.subprocess, "run", _run(returncode=1))
    assert desktop.notify("S", "B") is False


def test_darwin_notify_returns_false_when_unavailable(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: None)

    def boom(*_a, **_kw):
        raise AssertionError("must not shell out when unavailable")

    monkeypatch.setattr(desktop.subprocess, "run", boom)
    assert desktop.notify("S", "B") is False


def test_darwin_notify_returns_false_on_exception(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/bin/osascript")

    def raise_timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=10)

    monkeypatch.setattr(desktop.subprocess, "run", raise_timeout)
    assert desktop.notify("S", "B") is False


# --------------------------------------------------------------------------- #
# ask() -- terminal-notifier path
# --------------------------------------------------------------------------- #
def test_darwin_ask_uses_terminal_notifier_when_present(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil,
        "which",
        lambda name: (
            "/usr/bin/osascript" if name == "osascript" else "/usr/local/bin/terminal-notifier"
        ),
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="yes\n", stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    choice = desktop.ask("Q?", "context", ["yes", "no"])
    assert choice == "yes"
    args = captured["args"]
    assert args[0] == "terminal-notifier"
    assert "-actions" in args
    assert args[args.index("-actions") + 1] == "yes,no"


def test_darwin_ask_terminal_notifier_closed_returns_none(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
    monkeypatch.setattr(desktop.subprocess, "run", _run(returncode=0, stdout="@CLOSED\n"))
    assert desktop.ask("Q?", "context", ["yes", "no"]) is None


def test_darwin_ask_terminal_notifier_timeout_returns_none(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
    monkeypatch.setattr(desktop.subprocess, "run", _run(returncode=6, stdout="@TIMEOUT\n"))
    assert desktop.ask("Q?", "context", ["yes", "no"], timeout_ms=5000) is None


def test_darwin_ask_terminal_notifier_passes_timeout_seconds(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="yes\n")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    desktop.ask("Q?", "context", ["yes", "no"], timeout_ms=1500)
    args = captured["args"]
    assert args[args.index("-timeout") + 1] == "2"  # rounds up, never truncates to 0/1


# --------------------------------------------------------------------------- #
# ask() -- osascript display dialog fallback (no terminal-notifier)
# --------------------------------------------------------------------------- #
def test_darwin_ask_falls_back_to_osascript_dialog(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="button returned:Yes\n")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    choice = desktop.ask("Q?", "context", ["Yes", "No"])
    assert choice == "Yes"
    args = captured["args"]
    assert args[0] == "osascript"
    script = args[2]
    assert "display dialog" in script
    assert 'buttons {"Yes", "No"}' in script
    assert 'default button "No"' in script


def test_darwin_ask_dialog_cancel_returns_none(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        _run(returncode=1, stdout="", stderr="execution error: User canceled. (-128)"),
    )
    assert desktop.ask("Q?", "context", ["Yes", "No"]) is None


def test_darwin_ask_dialog_gave_up_returns_none(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        _run(returncode=0, stdout="button returned:No, gave up:true\n"),
    )
    # An unattended timeout auto-picks the default button under the hood, but that
    # is not an answer -- must not be reported as if the human clicked it.
    assert desktop.ask("Q?", "context", ["Yes", "No"], timeout_ms=30000) is None


def test_darwin_ask_dialog_passes_giving_up_after_seconds(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="button returned:Yes\n")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    desktop.ask("Q?", "context", ["Yes", "No"], timeout_ms=30000)
    assert "giving up after 30" in captured["args"][2]


def test_darwin_ask_dialog_no_timeout_omits_giving_up_after(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="button returned:Yes\n")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    desktop.ask("Q?", "context", ["Yes", "No"])  # default timeout_ms=0 -> block forever
    assert "giving up after" not in captured["args"][2]


def test_darwin_ask_dialog_caps_at_three_buttons_keeping_the_last(darwin, monkeypatch):
    # display dialog is an AppleScript hard limit of three buttons; the last option
    # is the caller's default/safe choice by convention, so it must survive the cut.
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="button returned:Dismiss\n"
        )

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    desktop.ask("Q?", "context", ["a", "b", "c", "Dismiss"])
    script = captured["args"][2]
    assert 'buttons {"b", "c", "Dismiss"}' in script
    assert 'default button "Dismiss"' in script


def test_darwin_ask_dialog_unknown_stdout_returns_none(darwin, monkeypatch):
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    monkeypatch.setattr(
        desktop.subprocess, "run", _run(returncode=0, stdout="not what we expected\n")
    )
    assert desktop.ask("Q?", "context", ["Yes", "No"]) is None


def test_darwin_ask_no_options_returns_none_without_shelling_out(darwin, monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: "/usr/bin/osascript")

    def boom(*_a, **_kw):
        raise AssertionError("must not shell out with no options")

    monkeypatch.setattr(desktop.subprocess, "run", boom)
    assert desktop.ask("Q?", "context", []) is None


# --------------------------------------------------------------------------- #
# Linux path -- unchanged, quick regression check
# --------------------------------------------------------------------------- #
def test_linux_available_unaffected_by_darwin_branch(monkeypatch):
    # _DARWIN is False under real test collection (this suite runs on Linux CI).
    assert desktop._DARWIN is False
    monkeypatch.setattr(
        desktop.shutil,
        "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    assert desktop.available() is True


def test_linux_notify_still_uses_notify_send(monkeypatch):
    monkeypatch.setattr(
        desktop.shutil,
        "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    assert desktop.notify("S", "B") is True
    assert captured["args"][0] == "notify-send"


def test_linux_ask_still_uses_notify_send_action(monkeypatch):
    monkeypatch.setattr(
        desktop.shutil,
        "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="no\n")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    assert desktop.ask("Q?", "context", ["yes", "no"]) == "no"
