"""Desktop notifications over D-Bus, for when an agent needs the human.

No GNOME extension and no gi bindings: org.freedesktop.Notifications is a
standard session-bus service that GNOME Shell already implements, and
notify-send is a client for it that ships with libnotify. That keeps this module
stdlib-only like the rest of the package, and working on any freedesktop
desktop rather than GNOME specifically.

Everything degrades to a no-op rather than raising: a headless box, an ssh
session, or a missing libnotify must not take the daemon down.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess

APP_NAME = "cabildo"
ICON = "dialog-information"
ICON_URGENT = "dialog-warning"


def available() -> bool:
    """A session bus and a client to talk to it. Both absent under systemd
    without an explicit DBUS_SESSION_BUS_ADDRESS, and over plain ssh."""
    return bool(shutil.which("notify-send")) and bool(
        os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        or os.path.exists(f"/run/user/{os.getuid()}/bus")
    )


def notify(summary: str, body: str, *, urgent: bool = False, timeout_ms: int = 12000) -> bool:
    """Pop a desktop notification. Returns whether it was actually sent.

    Urgent notifications use critical urgency, which GNOME keeps on screen until
    dismissed — appropriate when an agent is blocked waiting on an answer, and
    obnoxious for anything else.
    """
    if not available():
        return False
    args = [
        "notify-send",
        "--app-name", APP_NAME,
        "--icon", ICON_URGENT if urgent else ICON,
        "--urgency", "critical" if urgent else "normal",
        # Critical notifications ignore timeouts by spec; harmless to pass.
        "--expire-time", str(timeout_ms),
        summary,
        # notify-send renders a small HTML subset, so a body containing < or &
        # would silently vanish or mangle the rest of the message.
        _clip(html.escape(body)),
    ]
    try:
        return subprocess.run(args, capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _clip(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ask(summary: str, body: str, options: list[str], *, timeout_ms: int = 0) -> str | None:
    """Pop a notification with buttons and BLOCK until one is clicked.

    notify-send's --action implies --wait and prints the chosen action's name on
    stdout, so the whole round trip is one subprocess — no D-Bus signal
    plumbing, no gi bindings. Returns the chosen option, or None if the
    notification was dismissed, expired, or notifications aren't available.

    This blocks for as long as the human takes. Call it off the main loop.
    """
    if not available() or not options:
        return None
    args = ["notify-send", "--app-name", APP_NAME, "--icon", ICON_URGENT,
            "--urgency", "critical"]
    if timeout_ms:
        args += ["--expire-time", str(timeout_ms)]
    for o in options:
        args += ["--action", f"{o}={o}"]
    args += [summary, _clip(html.escape(body), 700)]
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=(timeout_ms / 1000 + 30) if timeout_ms else None)
    except Exception:
        return None
    choice = (p.stdout or "").strip()
    return choice if choice in options else None


def _context_line(m: dict) -> str:
    """Who is asking, from where, about what. A notification that just says
    'agent needs input' is noise: by the time you've found the right terminal
    you've lost the thread."""
    bits = []
    if m.get("project_name") or m.get("project"):
        proj = m.get("project_name") or os.path.basename(str(m.get("project") or ""))
        if proj:
            bits.append(proj)
    if m.get("to_session"):
        bits.append("direct to you")
    elif m.get("topic"):
        bits.append(f"#{m['topic']}")
    if m.get("urgent"):
        bits.append("blocked, waiting on you")
    return " · ".join(bits)


def notify_message(m: dict) -> bool:
    """Notify about one bus message addressed to the human, with enough context
    to be actionable without going to find the terminal it came from."""
    who = m.get("from_display") or "agent"
    urgent = bool(m.get("urgent"))
    mark = "⚠ " if urgent else "💬 "
    ctx = _context_line(m)
    summary = f"{mark}{who}" + (f" — {ctx}" if ctx else "")
    body = m.get("body") or ""
    if not m.get("urgent"):
        body += "\n\n(reply: cabildo say --to " + str(who).lstrip("@") + " ...)"
    return notify(summary, body, urgent=urgent)
