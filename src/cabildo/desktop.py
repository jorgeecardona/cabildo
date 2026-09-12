"""Desktop notifications, for when an agent needs the human.

Two backends, picked by `sys.platform`:

- **Linux**: org.freedesktop.Notifications over D-Bus. No GNOME extension and no
  gi bindings needed — it's a standard session-bus service that GNOME Shell
  already implements, and notify-send is a client for it that ships with
  libnotify. That keeps this module stdlib-only like the rest of the package,
  and working on any freedesktop desktop rather than GNOME specifically.
- **macOS**: `osascript` (ships with the OS, no install). Passive notifications
  use `display notification`; the button round-trip `request(action="ask")`
  needs uses `terminal-notifier` if it's on PATH — its `-actions` flag blocks
  and prints the clicked title as plain text, same idea as notify-send's
  `--action` — or else falls back to an `osascript display dialog`, which
  ships with the OS and blocks with `button returned:<choice>` on stdout.

Everything degrades to a no-op rather than raising: a headless box, an ssh
session, or a missing notifier must not take the daemon down.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys

APP_NAME = "cabildo"
ICON = "dialog-information"
ICON_URGENT = "dialog-warning"

_DARWIN = sys.platform == "darwin"


def available() -> bool:
    """Whether this platform has a working notifier.

    Linux: a session bus and a client to talk to it. Both absent under systemd
    without an explicit DBUS_SESSION_BUS_ADDRESS, and over plain ssh.
    macOS: osascript, which ships with the OS — absent only in some minimal
    CI/container images, so still worth checking rather than assuming.
    """
    if _DARWIN:
        return bool(shutil.which("osascript"))
    return bool(shutil.which("notify-send")) and bool(
        os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.path.exists(f"/run/user/{os.getuid()}/bus")
    )


def notify(summary: str, body: str, *, urgent: bool = False, timeout_ms: int = 12000) -> bool:
    """Pop a desktop notification. Returns whether it was actually sent.

    Urgent notifications use critical urgency, which GNOME keeps on screen until
    dismissed — appropriate when an agent is blocked waiting on an answer, and
    obnoxious for anything else. macOS has no equivalent "stay until dismissed"
    urgency for a passive banner, so `urgent` there only affects the sound.
    """
    if not available():
        return False
    if _DARWIN:
        return _darwin_notify(summary, body, urgent=urgent)
    args = [
        "notify-send",
        "--app-name",
        APP_NAME,
        "--icon",
        ICON_URGENT if urgent else ICON,
        "--urgency",
        "critical" if urgent else "normal",
        # Critical notifications ignore timeouts by spec; harmless to pass.
        "--expire-time",
        str(timeout_ms),
        summary,
        # notify-send renders a small HTML subset, so a body containing < or &
        # would silently vanish or mangle the rest of the message.
        _clip(html.escape(body)),
    ]
    try:
        return subprocess.run(args, capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _osa_quote(text: str) -> str:
    """Escape a string for embedding in an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _darwin_notify(summary: str, body: str, *, urgent: bool = False) -> bool:
    """`display notification` — fire-and-forget, no buttons, no blocking."""
    script = f'display notification "{_osa_quote(_clip(body))}" with title "{_osa_quote(summary)}"'
    if urgent:
        script += ' sound name "Basso"'
    try:
        return (
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10).returncode
            == 0
        )
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
    if _DARWIN:
        return _darwin_ask(summary, body, options, timeout_ms=timeout_ms)
    args = ["notify-send", "--app-name", APP_NAME, "--icon", ICON_URGENT, "--urgency", "critical"]
    if timeout_ms:
        args += ["--expire-time", str(timeout_ms)]
    for o in options:
        args += ["--action", f"{o}={o}"]
    args += [summary, _clip(html.escape(body), 700)]
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000 + 30) if timeout_ms else None,
        )
    except Exception:
        return None
    choice = (p.stdout or "").strip()
    return choice if choice in options else None


def _darwin_ask(summary: str, body: str, options: list[str], *, timeout_ms: int) -> str | None:
    """The macOS side of the button round-trip.

    Prefers terminal-notifier when it's on PATH: since 3.0.0 its `-actions` flag
    blocks until the user picks one and prints the chosen title verbatim to
    stdout (plain text, not JSON — there's no `-json` flag). Falls back to an
    `osascript display dialog`, which ships with the OS and needs nothing extra
    installed, at the cost of a modal window instead of a notification banner.
    """
    if shutil.which("terminal-notifier"):
        return _terminal_notifier_ask(summary, body, options, timeout_ms=timeout_ms)
    return _osascript_ask(summary, body, options, timeout_ms=timeout_ms)


def _terminal_notifier_ask(
    summary: str, body: str, options: list[str], *, timeout_ms: int
) -> str | None:
    args = [
        "terminal-notifier",
        "-title",
        summary,
        "-message",
        _clip(body, 700),
        "-actions",
        ",".join(options),
    ]
    if timeout_ms:
        # terminal-notifier's -timeout is whole seconds; round up so a sub-second
        # remainder isn't silently dropped into an instant timeout.
        args += ["-timeout", str(max(1, (timeout_ms + 999) // 1000))]
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000 + 30) if timeout_ms else None,
        )
    except Exception:
        return None
    # Anything other than the literal clicked action title — @ACTIONCLICKED (body
    # clicked, not a button), @CLOSED (dismissed), @TIMEOUT (exit 6) — is "no answer".
    choice = (p.stdout or "").strip()
    return choice if choice in options else None


#: display dialog is a hard AppleScript limit, not a style choice — Apple's own docs
#: cap it at three. terminal-notifier (above) has no such ceiling, so this path only
#: matters when terminal-notifier isn't installed; keeping the *last* N options rather
#: than the first preserves the caller's default/safe choice, which by convention
#: (mirroring notify-send's own ordering) is listed last.
_MAX_DIALOG_BUTTONS = 3


def _osascript_ask(summary: str, body: str, options: list[str], *, timeout_ms: int) -> str | None:
    buttons = options[-_MAX_DIALOG_BUTTONS:]
    quoted = ", ".join(f'"{_osa_quote(o)}"' for o in buttons)
    script = (
        f'display dialog "{_osa_quote(_clip(body, 700))}" '
        f'with title "{_osa_quote(summary)}" '
        f'buttons {{{quoted}}} default button "{_osa_quote(buttons[-1])}"'
    )
    if timeout_ms:
        script += f" giving up after {max(1, timeout_ms // 1000)}"
    try:
        p = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000 + 30) if timeout_ms else None,
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None  # Cancel / Esc / Cmd-. raises AppleScript error -128
    out = p.stdout.strip()
    if "gave up:true" in out:
        # `giving up after` elapsed with no click; AppleScript still reports the
        # default button in `button returned`, but that's an auto-pick, not an
        # answer — surfacing it as one would be the same unearned "he answered"
        # that notify_message's docstring warns against for urgency.
        return None
    m = re.search(r"button returned:([^,]+)", out)
    if not m:
        return None
    choice = m.group(1).strip()
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
            break  # prose after the dump is a new thought; leave it
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
