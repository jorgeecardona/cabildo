"""siblings.py: start another Claude next to the calling one, in the session host.

A sibling is an interactive Claude the host owns. The agent that starts it can give it a first
prompt, type to it later, and list the siblings it started. A person can attach to one with
``cabildo attach``. Two limits stop a runaway chain: a cap on the siblings agents have started,
and a depth limit carried in the environment.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping

from . import host, host_client

CLAUDE = "claude"  # the CLI a sibling runs; tests point this at a stand-in
MAX_SIBLINGS = 4  # hosted sessions started by agents, all parents together
MAX_DEPTH = 2  # a sibling may start one more level, not a third
DEPTH_VAR = "CABILDO_SIBLING_DEPTH"


class SiblingError(Exception):
    """The sibling cannot be started or reached. The message says why."""


def depth(env: Mapping[str, str] | None = None) -> int:
    """How many siblings deep the calling Claude is: 0 for one a person started."""
    raw = (os.environ if env is None else env).get(DEPTH_VAR, "0")
    return int(raw) if raw.isdigit() else 0


def brief(parent_id: str, parent_handle: str, prompt: str) -> str:
    """The first message of a sibling: who started it and how to answer, then the task."""
    who = f"{parent_handle} (session {parent_id})" if parent_handle else f"session {parent_id}"
    return (
        f"You were started by {who} with cabildo. Answer them with the cabildo `send` tool, "
        f"to that session id, when you have a result or a question.\n\n{prompt}"
    )


def start(
    *,
    parent_id: str,
    parent_handle: str = "",
    prompt: str = "",
    cwd: str = "",
    permission_mode: str = "",
) -> host.Record:
    """Start a sibling in ``cwd`` (default: the caller's folder) and return its record.

    Raises:
        SiblingError: when a limit is reached, the folder is missing, or the host refuses.
    """
    level = depth() + 1
    if level > MAX_DEPTH:
        raise SiblingError(f"sibling depth {level} is over the limit ({MAX_DEPTH})")
    where = os.path.abspath(cwd or os.getcwd())
    if not os.path.isdir(where):
        raise SiblingError(f"no such directory: {where}")
    live = [r for r in host_client.list_sessions() if r.parent]
    if len(live) >= MAX_SIBLINGS:
        raise SiblingError(f"at the {MAX_SIBLINGS}-sibling cap; {len(live)} still running")
    command = [shutil.which(CLAUDE) or CLAUDE]
    if permission_mode:
        command += ["--permission-mode", permission_mode]
    if prompt.strip():
        command.append(brief(parent_id, parent_handle, prompt.strip()))
    env = {**host_client.clean_env(), DEPTH_VAR: str(level)}
    try:
        return host_client.start_session(command, cwd=where, env=env, parent=parent_id)
    except host_client.ClientError as exc:
        raise SiblingError(str(exc)) from exc


def send(host_id: str, text: str, *, submit: bool = True) -> None:
    """Type ``text`` into a sibling and press Enter (unless ``submit`` is false).

    Raises:
        SiblingError: when the host has no such session.
    """
    try:
        host_client.send_input(host_id, text, submit=submit)
    except host_client.ClientError as exc:
        raise SiblingError(str(exc)) from exc
