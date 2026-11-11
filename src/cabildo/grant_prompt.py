"""Access requests addressed to the human pop with Grant / Deny buttons.

``request_access`` DMs the human a knock that starts ``🔔 access request #<id>``. The daemon
sees it in its notify pass and, instead of a plain message popup, calls ``desktop.ask`` on a
thread. A click decides the grant and DMs the requester, the same as ``grant``/``deny``.
"""

from __future__ import annotations

import re
import threading

from . import desktop, store

KNOCK = re.compile(r"^🔔 access request #(\d+):")
GRANT, DENY = "Grant", "Deny"


def grant_id(body: str) -> int | None:
    """The grant id of an access-request knock, or None for any other message."""
    m = KNOCK.match(body or "")
    return int(m.group(1)) if m else None


def decide(conn, gid: int, choice: str | None) -> str | None:
    """Apply a button choice to a still-pending grant. Returns the new status, or None.

    Does nothing when the popup was dismissed or the grant was already decided elsewhere.
    """
    status = {GRANT: "granted", DENY: "denied"}.get(choice or "")
    g = store.get_grant(conn, gid)
    if status is None or g is None or g["status"] != "requested":
        return None
    store.decide_grant(conn, gid, status=status, by=store.HUMAN_SESSION)
    if g.get("requested_by"):
        store.post_message(
            conn,
            from_session=store.HUMAN_SESSION,
            from_handle=store.HUMAN_HANDLE,
            body=f"access request #{gid} ({g['capability']} {g['resource_kind']} "
            f"'{g['resource']}') → {status}",
            to_session=g["requested_by"],
            urgent=status != "granted",
        )
    conn.commit()
    return status


def text(g: dict) -> tuple[str, str]:
    """The popup title and body for one grant: who asks, for what, and why."""
    target = (
        f"your project ({g['resource']})"
        if g["resource_kind"] == "project"
        else f"#{g['resource']}"
    )
    summary = f"{g['grantee']} wants to {g['capability']} to {target}"
    body = g.get("note") or "(no reason given)"
    return summary, f"{body}\n\nrequest #{g['id']}"


def prompt(m: dict) -> bool:
    """Pop Grant / Deny for a knock message, on a thread. False when ``m`` is not a knock."""
    gid = grant_id(str(m.get("body") or ""))
    if gid is None:
        return False

    def run() -> None:
        conn = store.connect()
        try:
            g = store.get_grant(conn, gid)
            if g is None or g["status"] != "requested":
                return
            summary, body = text(g)
            decide(conn, gid, desktop.ask(summary, body, [GRANT, DENY]))
        finally:
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return True


def reprompt_pending(conn) -> int:
    """Pop Grant / Deny again for every request still pending. Returns how many.

    The daemon calls this at start: a popup lives in the daemon, so a restart or a dismissed
    popup must not leave a request that only the CLI can answer.
    """
    rows = conn.execute("SELECT * FROM grants WHERE status='requested' ORDER BY id").fetchall()
    for g in rows:
        target = "your project" if g["resource_kind"] == "project" else f"#{g['resource']}"
        note = f" — {g['note']}" if g["note"] else ""
        body = (
            f"🔔 access request #{g['id']}: project '{g['grantee']}' asks to "
            f"{g['capability']} {target}{note}."
        )
        prompt({"body": body})
    return len(rows)
