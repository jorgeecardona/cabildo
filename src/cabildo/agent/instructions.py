"""Project instructions: the nearest ``AGENTS.md``, as the agents.md convention says."""

from __future__ import annotations

from pathlib import Path

MAX_CHARS = 8000  # a small local model has a small context window


def find_agents_md(cwd: Path, root: Path) -> Path | None:
    """Return the nearest ``AGENTS.md`` from ``cwd`` up to ``root``, or None."""
    cwd, root = cwd.resolve(), root.resolve()
    for folder in (cwd, *cwd.parents):
        candidate = folder / "AGENTS.md"
        if candidate.is_file():
            return candidate
        if folder == root:
            break
    return None


def with_instructions(system: str, cwd: Path, root: Path) -> tuple[str, Path | None]:
    """Append the nearest ``AGENTS.md`` to ``system``. Returns the prompt and the file used."""
    path = find_agents_md(cwd, root)
    if path is None:
        return system, None
    text = path.read_text(errors="replace")
    note = "\n[truncated]" if len(text) > MAX_CHARS else ""
    return f"{system}\n\nProject instructions ({path.name}):\n{text[:MAX_CHARS]}{note}", path
