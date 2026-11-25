"""Read and edit the cells of a notebook that a Jupyter runner serves.

An edit is a read, a change and a write of the ``.ipynb`` through the server's contents API
(``/api/contents``). With ``jupyter-collaboration`` in the runner (the default) the server sees
the new file within about a second and updates the notebook that you have open in JupyterLab, so
you watch the agent work.

Two things keep an edit from losing yours:

- It waits until the file has not changed for ``QUIET_SECONDS``. JupyterLab saves your edits about
  a second after you stop typing, so a recent change on disk means you are typing, and your
  newest keystrokes exist only in the browser.
- It checks that the file did not change between the read and the write, and refuses when it did.

Cells are addressed by their nbformat ``id`` or by their position (``"0"``, ``"-1"``).
"""

from __future__ import annotations

import contextlib
import fcntl
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from . import jupyter
from .jupyter import RunnerError, RunnerState, RunResult

QUIET_SECONDS = 2.0  # the file must be this old before an edit
SETTLE_SECONDS = 10.0  # how long to wait for it before giving up
PREVIEW = 300  # characters of source and output that ``cells`` shows
CELL_TYPES = ("code", "markdown", "raw")


@dataclass(frozen=True)
class CellView:
    """One cell as ``cells`` lists it."""

    index: int
    id: str
    cell_type: str
    source: str
    execution_count: int | None = None
    outputs: int = 0  # how many outputs the cell holds
    output_text: str = ""  # the start of its text output


def _state(name: str) -> RunnerState:
    state = jupyter.running(name)
    if state is None:
        raise RunnerError(f"runner {name!r} is not running: start it first")
    return state


def _url(path: str) -> str:
    return f"/api/contents/{quote(path)}"


def _modified(state: RunnerState, path: str) -> str:
    return str(jupyter._http(state, "GET", f"{_url(path)}?content=0")["last_modified"])


def _age(stamp: str) -> float:
    """Seconds since a ``last_modified`` stamp such as ``2026-11-25T10:00:00.5Z``."""
    then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (datetime.now(UTC) - then).total_seconds()


def _settle(state: RunnerState, path: str) -> None:
    """Wait until the file has not changed for ``QUIET_SECONDS``.

    Raises:
        RunnerError: it kept changing for ``SETTLE_SECONDS``: someone is typing in it.
    """
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        age = _age(_modified(state, path))
        if age >= QUIET_SECONDS:
            return
        if time.monotonic() + (QUIET_SECONDS - age) > deadline:
            raise RunnerError(
                f"{path} was saved {age:.1f} s ago: someone is editing it. Try again in a few seconds"
            )
        time.sleep(max(QUIET_SECONDS - age, 0.1) + 0.05)


def _load(state: RunnerState, path: str) -> tuple[dict[str, Any], str]:
    """The notebook as nbformat JSON, and its ``last_modified``.

    Raises:
        RunnerError: there is no such notebook.
    """
    model = jupyter._http(state, "GET", f"{_url(path)}?content=1&type=notebook")
    notebook = model["content"]
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise RunnerError(f"{path} is not a notebook")
    return notebook, str(model["last_modified"])


def _prepare(notebook: dict[str, Any]) -> None:
    """Give every cell an id (nbformat 4.5), so cells can be named and merged."""
    if notebook.get("nbformat", 4) == 4 and notebook.get("nbformat_minor", 0) < 5:
        notebook["nbformat_minor"] = 5
    for cell in notebook["cells"]:
        cell.setdefault("id", uuid.uuid4().hex[:8])


def _save(state: RunnerState, path: str, notebook: dict[str, Any], before: str) -> None:
    """Write ``notebook`` unless the file changed since ``before``.

    Raises:
        RunnerError: it changed: read it again and retry.
    """
    _prepare(notebook)
    if _modified(state, path) != before:
        raise RunnerError(f"{path} changed while it was being edited: try again")
    body = {"type": "notebook", "format": "json", "content": notebook}
    jupyter._http(state, "PUT", _url(path), body)


def _text(source: object) -> str:
    return "".join(source) if isinstance(source, list) else str(source or "")


def _view(index: int, cell: dict[str, Any]) -> CellView:
    outputs = cell.get("outputs", [])
    shown = "".join(
        _text(o.get("text") or o.get("data", {}).get("text/plain", "")) for o in outputs
    )
    return CellView(
        index=index,
        id=str(cell.get("id", "")),
        cell_type=str(cell.get("cell_type", "code")),
        source=_text(cell.get("source"))[:PREVIEW],
        execution_count=cell.get("execution_count"),
        outputs=len(outputs),
        output_text=shown[:PREVIEW],
    )


def _index(notebook: dict[str, Any], ref: str) -> int:
    """Position of the cell named ``ref``: an nbformat id, or a number (negative counts back).

    Raises:
        RunnerError: there is no such cell.
    """
    cells = notebook["cells"]
    for i, cell in enumerate(cells):
        if cell.get("id") == ref:
            return i
    try:
        i = int(ref)
    except ValueError:
        i = len(cells)
    i = i + len(cells) if i < 0 else i
    if not 0 <= i < len(cells):
        raise RunnerError(f"no cell {ref!r} (the notebook has {len(cells)})")
    return i


@contextlib.contextmanager
def _edit_lock(name: str) -> Iterator[None]:
    """One writer at a time per runner: the read, change and write of an edit."""
    with (jupyter.runner_dir(name) / "notebook.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def cells(name: str, notebook: str) -> list[CellView]:
    """The cells of ``notebook``, in order. Source and output are cut to ``PREVIEW`` characters.

    Raises:
        RunnerError: the runner is not running, or the notebook is missing.
    """
    state = _state(name)
    nb, _ = _load(state, jupyter.notebook_path(notebook))
    _prepare(nb)
    return [_view(i, c) for i, c in enumerate(nb["cells"])]


def _new_cell(source: str, cell_type: str) -> dict[str, Any]:
    if cell_type not in CELL_TYPES:
        raise RunnerError(f"cell_type {cell_type!r}: use one of {', '.join(CELL_TYPES)}")
    cell: dict[str, Any] = {
        "id": uuid.uuid4().hex[:8], "cell_type": cell_type, "metadata": {}, "source": source,
    }  # fmt: skip
    if cell_type == "code":
        cell |= {"outputs": [], "execution_count": None}
    return cell


def _change(name: str, notebook: str, change: Callable[[dict[str, Any]], int]) -> CellView:
    """Settle, then read, change and write the notebook under the writer lock.

    ``change`` edits the notebook in place and returns the index of the cell it touched.
    """
    path = jupyter.notebook_path(notebook)
    state = _state(name)
    with _edit_lock(name):
        _settle(state, path)
        nb, before = _load(state, path)
        _prepare(nb)
        index = change(nb)
        _save(state, path, nb, before)
        return _view(index, nb["cells"][index]) if nb["cells"] else CellView(-1, "", "", "")


def edit(
    name: str, notebook: str, cell: str, source: str, cell_type: str | None = None
) -> CellView:
    """Replace the source of one cell (and its type when ``cell_type`` is given).

    Outputs of a code cell stay until it runs again.
    """

    def change(nb: dict[str, Any]) -> int:
        i = _index(nb, cell)
        target = nb["cells"][i]
        target["source"] = source
        if cell_type and cell_type != target["cell_type"]:
            fresh = _new_cell(source, cell_type)
            fresh["id"] = target["id"]
            nb["cells"][i] = fresh
        return i

    return _change(name, notebook, change)


def insert(
    name: str, notebook: str, source: str, position: int = -1, cell_type: str = "code"
) -> CellView:
    """Add a cell at ``position`` (0 first; -1, the default, last). The notebook is made when
    it does not exist.
    """

    def change(nb: dict[str, Any]) -> int:
        at = len(nb["cells"]) if position < 0 else min(position, len(nb["cells"]))
        nb["cells"].insert(at, _new_cell(source, cell_type))
        return at

    state = _state(name)
    jupyter._ensure_notebook(state, jupyter.notebook_path(notebook))
    return _change(name, notebook, change)


def delete(name: str, notebook: str, cell: str) -> CellView:
    """Remove one cell and return what it was."""
    gone: list[CellView] = []

    def change(nb: dict[str, Any]) -> int:
        i = _index(nb, cell)
        gone.append(_view(i, nb["cells"].pop(i)))
        return min(i, len(nb["cells"]) - 1)

    _change(name, notebook, change)
    return gone[0]


def _with_outputs(cell: dict[str, Any], result: RunResult) -> None:
    cell["outputs"] = list(result.outputs)
    cell["execution_count"] = result.execution_count or None


def run_cell(
    name: str, notebook: str, cell: str, timeout: float = 30.0
) -> tuple[RunResult, CellView]:
    """Run one cell on the notebook's kernel and store its outputs in the cell.

    The notebook you have open shows them within about a second. Returns what ``run`` returns
    and the cell as it is now.

    Raises:
        RunnerError: no such cell, a kernel failure, or the notebook changed during the write.
    """
    state = _state(name)
    path = jupyter.notebook_path(notebook)
    nb, _ = _load(state, path)
    _prepare(nb)
    source = _text(nb["cells"][_index(nb, cell)].get("source"))
    result = jupyter.run(name, source, timeout, notebook)

    def change(nb: dict[str, Any]) -> int:
        i = _index(nb, cell)
        if nb["cells"][i]["cell_type"] == "code":
            _with_outputs(nb["cells"][i], result)
        return i

    return result, _change(name, notebook, change)


def append_run(name: str, notebook: str, code: str, result: RunResult) -> CellView:
    """Add a code cell holding ``code`` and the outputs ``result`` made, at the end."""

    def change(nb: dict[str, Any]) -> int:
        new = _new_cell(code, "code")
        _with_outputs(new, result)
        nb["cells"].append(new)
        return len(nb["cells"]) - 1

    state = _state(name)
    jupyter._ensure_notebook(state, jupyter.notebook_path(notebook))
    return _change(name, notebook, change)
