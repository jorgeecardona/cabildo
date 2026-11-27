"""The gateway's upstreams, for the mode line: who serves this session, and each one's counts.

Nothing is shown unless ``[gateway]`` is configured, Claude Code's settings point at it, and it
answers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

from cabildo import config, gateway, gateway_usage

from .usage import fmt_count, fmt_money


@dataclass(frozen=True)
class SessionUpstream:
    """The upstream that last served one session."""

    session_id: str
    upstream: str


@dataclass(frozen=True)
class View:
    """The upstreams in the order of the config file, and which one served each session."""

    upstreams: tuple[gateway.UpstreamStatus, ...]
    sessions: tuple[SessionUpstream, ...] = ()
    spent: tuple[gateway_usage.Spent, ...] = ()  # the upstreams that served something

    def used(self, up: gateway.UpstreamStatus) -> str:
        """``today $3.20 · 7d $12.40`` for ``up``; "" when it never served a reply.

        A passthrough upstream shows tokens: its replies are part of a subscription.
        """
        found = next((s for s in self.spent if s.upstream == up.name), None)
        return spent_text(found, priced=up.kind != "passthrough") if found else ""

    def serving(self, session_ids: tuple[str, ...]) -> gateway.UpstreamStatus | None:
        """The upstream of the first of ``session_ids`` the gateway saw; None if it saw none."""
        seen = {s.session_id: s.upstream for s in self.sessions}
        name = next((seen[i] for i in session_ids if i in seen), None)
        return next((u for u in self.upstreams if u.name == name), None)


def spent_text(spent: gateway_usage.Spent, *, priced: bool) -> str:
    """Today and 7 days: estimated cost when ``priced``, else the number of tokens."""
    parts: list[str] = []
    for label, window in (("today", spent.today), ("7d", spent.week)):
        if not priced:
            value = f"{fmt_count(window.tokens)} tokens"
        elif window.unpriced:
            value = f"{fmt_money(window.cost)} + {fmt_count(window.unpriced)} tokens unpriced"
        else:
            value = fmt_money(window.cost)
        parts.append(f"{label} {value}")
    return " · ".join(parts)


def parse(data: Mapping[str, Any]) -> View | None:
    """The ``View`` in a status reply; None when it does not have the expected shape."""
    known = {f.name for f in fields(gateway.UpstreamStatus)}
    try:
        ups = tuple(
            gateway.UpstreamStatus(**{k: v for k, v in row.items() if k in known})
            for row in data["upstreams"]
        )
        sessions = data.get("sessions") or {}  # a gateway older than the panel sends none
        return View(ups, tuple(SessionUpstream(str(k), str(v)) for k, v in sessions.items()))
    except (KeyError, TypeError, AttributeError):
        return None


class Reader:
    """Asks the gateway. It keeps the keys, so a 1Password token is read only once."""

    def __init__(self) -> None:
        self._keys = gateway.Keys()

    def read(self) -> View | None:
        """The gateway's status now; None when this machine's sessions do not use it. Blocks."""
        table = config.gateway_table()
        if not table:
            return None
        try:
            settings = gateway.parse(table)
            if not gateway.claude_uses(gateway.claude_settings_path(), settings.port):
                return None
            token = self._keys.get(settings.token) if settings.token else None
            view = parse(gateway.fetch_status(settings.port, token))
        except (gateway.GatewayError, OSError, ValueError):
            return None
        if view is None:
            return None
        names = tuple(u.name for u in view.upstreams)
        return replace(view, spent=gateway_usage.spent(names))
