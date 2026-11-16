"""Frozen data types shared by the client, the tools, and the loop."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    """One tool call requested by the model."""

    id: str
    name: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class Message:
    """One chat message. ``calls`` is set only on assistant messages."""

    role: Role
    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    call_id: str = ""  # set on tool messages: the ``ToolCall.id`` this answers


class Claims(Protocol):
    """Advisory locks on paths, so two agents do not edit the same file."""

    def acquire(self, path: Path) -> str | None:
        """Take a claim on ``path``. Returns None on success, else the holder's name."""
        ...

    def release_all(self) -> None:
        """Release every claim taken."""
        ...


class NoClaims:
    """Claims that always succeed. Used when no bus is available."""

    def acquire(self, path: Path) -> str | None:
        """Always succeed."""
        return None

    def release_all(self) -> None:
        """Nothing to release."""


@dataclass(frozen=True)
class Context:
    """What a tool needs to run."""

    cwd: Path
    claims: Claims = field(default_factory=NoClaims)


@dataclass(frozen=True)
class Tool:
    """A tool the model can call. ``writes`` tools take a claim before they run."""

    name: str
    description: str
    parameters: Mapping[str, object]  # JSON Schema object
    run: Callable[[Mapping[str, object], Context], str]
    writes: bool = False
    write_path_arg: str = "path"


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible chat endpoint."""

    base_url: str = "http://localhost:8000/v1"
    model: str = "Qwen3-0.6B"
    timeout_s: float = 120.0
