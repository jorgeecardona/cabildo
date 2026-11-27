"""How to open a conversation of the list: the workspace and ``cabildo panel`` both use it."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from cabildo import host
from cabildo.workspace import sessions
from cabildo.workspace.agents import AgentState
from cabildo.workspace.launch import Config
from cabildo.workspace.sessions import Row


@dataclass(frozen=True)
class Route:
    """How to open one conversation.

    ``attach``: Claude runs it as a background agent, ``command`` attaches to it.
    ``hosted``: the session host runs it (``host``). ``resume``: ``command`` starts Claude on it.
    """

    kind: Literal["attach", "hosted", "resume"]
    session_id: str  # the session the viewer shows
    command: tuple[str, ...]  # empty for ``hosted``
    cwd: str | None  # where Claude starts; None: where the caller runs
    host: host.Record | None = None


def route(
    row: Row,
    here: str,
    agent: AgentState | None,
    loaded: bool,
    hosted: Callable[[], Iterable[host.Record]],
    config: Config,
    claude: str,
) -> Route | None:
    """How to open ``row`` from the directory ``here``; None when its directory is gone.

    A continuation (made by ← in Claude) is a background session whose own transcript holds
    no messages, so ``--resume`` cannot load it: it is attached. So is it before the first
    ``claude agents`` answer (``loaded`` false), when ``agent`` says nothing yet. Conversations
    of ``here`` start there; another directory's start in their own.
    """
    s = row.resumable
    local = s.path.parent in sessions.project_dirs(here)
    cwd = None if local else sessions.launch_dir(s)
    attach = (agent is not None and agent.attachable) or (not loaded and len(row.chain) > 1)
    if attach:  # the agent runs on its own: any directory will do
        agent_id = agent.agent_id if agent else row.head.session_id[:8]
        return Route("attach", row.head.session_id, (claude, "attach", agent_id), cwd)
    if not local and cwd is None:
        return None
    owned = next((h for h in hosted() if h.session_id in row.ids), None)
    if owned is not None:  # the session host already runs it: show that one
        return Route("hosted", s.session_id, (), cwd, owned)
    command = config.launch_for(row.meta.group).command(claude, "--resume", s.session_id)
    return Route("resume", s.session_id, command, cwd)
