"""SIGHUP makes the daemon finish what it runs, take nothing new, and ask to be started again."""

from __future__ import annotations

import os
import signal
import threading
import time

from cabildo import daemon, store

ARGS = ["--no-monitor", "--no-notify", "--interval", "0.05"]


def hup_when(ready: threading.Event | None = None) -> None:
    """Send SIGHUP from another thread once the daemon has set its handler (and ``ready`` is set).

    The daemon runs on the main thread, the only one that can take a signal handler. Before its
    handler is set, SIGHUP would end the test process.
    """
    before = signal.getsignal(signal.SIGHUP)

    def send() -> None:
        deadline = time.monotonic() + 10
        while signal.getsignal(signal.SIGHUP) == before and time.monotonic() < deadline:
            time.sleep(0.02)
        if ready is not None:
            ready.wait(10)
        os.kill(os.getpid(), signal.SIGHUP)

    threading.Thread(target=send, daemon=True).start()


def test_an_idle_daemon_returns_the_reload_code_and_restores_the_signal_handler(home):
    before = signal.getsignal(signal.SIGHUP)
    hup_when()
    assert daemon.main(ARGS) == daemon.RELOAD_EXIT
    assert signal.getsignal(signal.SIGHUP) == before


def test_a_running_intent_finishes_and_a_queued_one_waits_for_the_next_daemon(home, monkeypatch):
    conn = store.connect()
    for n in (1, 2):
        store.request_intent(
            conn,
            from_session="s",
            from_handle="h",
            project="/p",
            action="ask",
            params={"n": n},
        )
    release, started = threading.Event(), threading.Event()
    ran: list[int] = []

    def slow(intent: dict, _ctx: object) -> None:
        ran.append(intent["id"])
        started.set()
        release.wait(10)
        c = store.connect()
        store.finish_intent(c, intent["id"], status="done", result={"ok": True})
        c.close()

    monkeypatch.setattr(daemon, "_handle_on_thread", slow)
    waited: list[bool] = []

    def let_it_finish() -> None:
        started.wait(10)
        time.sleep(1)  # long enough for the daemon to have seen SIGHUP and kept waiting
        waited.append(len(ran) == 1)
        release.set()

    threading.Thread(target=let_it_finish, daemon=True).start()
    hup_when(started)
    assert daemon.main([*ARGS, "--workers", "1"]) == daemon.RELOAD_EXIT
    assert waited == [True], "the daemon must wait for the running intent"
    assert len(ran) == 1, "no second intent starts after SIGHUP"
    left = conn.execute("select count(*) from intents where status='pending'").fetchone()[0]
    assert left == 1
