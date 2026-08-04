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
import re
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


#: Lines that are machine detail, not news. A popup showing the first 400 characters of a `git
#: status` dump is 400 characters of nothing: the reader cannot see where it starts, where it ends,
#: or what they are supposed to do about it.
_NOISE = re.compile(r"^\s*(?:[MADRCU?!]{1,2}\s+\S|[-*]\s|\d+\s+files?\s+changed)")


def _headline(body: str) -> tuple[str, str]:
    """Split a message into (what happened, what to do about it).

    Bus messages are written for an agent's inbox, where a file list is exactly right. A desktop
    popup is a different medium with a different reader: he is not at that terminal, cannot scroll,
    and did not ask. So the popup gets the sentence, and the count of the rest.
    """
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return "", ""
    lead, detail = [], 0
    for ln in lines:
        if _NOISE.match(ln):
            detail += 1
        elif detail:
            break                       # prose after the dump is a new thought; leave it
        else:
            lead.append(ln)
    head = " ".join(" ".join(lead).split())
    # The first sentence carries the verdict; the rest is usually the same fact restated for a
    # machine. Cut at punctuation, not at a character count that lands mid-path.
    cut = re.split(r"(?<=[.!?:])\s", head, maxsplit=1)
    head = cut[0] if cut and len(cut[0]) >= 30 else head
    return head[:180], (f"{detail} file(s) listed" if detail else "")


def notify_message(m: dict) -> bool:
    """Notify about one bus message addressed to the human.

    Two things this deliberately does NOT do. It does not echo the body — see `_headline`. And it
    does not raise an intent result to critical urgency just because the message is flagged urgent:
    urgent on the bus means "interrupt the agent's turn", and GNOME's critical means "stay on this
    human's screen until dismissed". Those are different claims, and conflating them is how a land
    that failed because his own tree was dirty ended up as an undismissable alert.
    """
    who = m.get("from_display") or "agent"
    head, rest = _headline(m.get("body") or "")
    proj = m.get("project_name") or os.path.basename(str(m.get("project") or ""))

    # Only a person asking for something gets to hold the screen. The daemon reporting on work it
    # was asked to do is news, however badly that work went.
    from_daemon = str(who).lstrip("@").lower() == "daemon"
    urgent = bool(m.get("urgent")) and not from_daemon

    mark = "⚠" if urgent else ("❌" if head.startswith("❌") else "💬")
    head = head.lstrip("❌✅⚠ ").strip()
    summary = f"{mark} {proj or who}" + (f" · {who}" if proj and not from_daemon else "")

    parts = [head] if head else []
    if rest:
        parts.append(f"({rest})")
    parts.append(f"→ cabildo log -n 5    ·    cabildo say --to {str(who).lstrip('@')} …")
    return notify(summary, "\n".join(parts), urgent=urgent)
