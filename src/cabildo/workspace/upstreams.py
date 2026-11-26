"""The gateway's upstreams, for the mode line: who serves this session, and each one's counts.

Nothing is shown unless ``[gateway]`` is configured, Claude Code's settings point at it, and it
answers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from cabildo import config, gateway


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

    def serving(self, session_ids: tuple[str, ...]) -> gateway.UpstreamStatus | None:
        """The upstream of the first of ``session_ids`` the gateway saw; None if it saw none."""
        seen = {s.session_id: s.upstream for s in self.sessions}
        name = next((seen[i] for i in session_ids if i in seen), None)
        return next((u for u in self.upstreams if u.name == name), None)


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
            return parse(gateway.fetch_status(settings.port, token))
        except (gateway.GatewayError, OSError, ValueError):
            return None
