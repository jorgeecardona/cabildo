"""jupyter_cells.py and the notebook guard, against a scripted contents API."""

from __future__ import annotations

import copy
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cabildo import guard, jupyter, jupyter_cells


class Contents:
    """An in-memory ``/api/contents`` for one runner: files by path, with ``last_modified``."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.stamp: dict[str, datetime] = {}
        self.on_get: Any = None  # called after a metadata read, to simulate a concurrent save

    def put(self, path: str, notebook: dict[str, Any], age: float = 60.0) -> None:
        self.files[path] = copy.deepcopy(notebook)
        self.stamp[path] = datetime.now(UTC) - timedelta(seconds=age)

    def __call__(self, _state, method, path, body=None, timeout=10.0):
        route, _, query = path.partition("?")
        name = route.removeprefix("/api/contents/")
        if method == "GET":
            if name not in self.files:
                raise jupyter.RunnerError(f"GET {path}: HTTP 404")
            model = {"last_modified": self.stamp[name].isoformat().replace("+00:00", "Z")}
            if "content=1" in query:
                model["content"] = copy.deepcopy(self.files[name])
            if self.on_get:
                self.on_get(self)
            return model
        if method == "PUT":
            content = body["content"] if body["type"] == "notebook" else None
            self.files[name] = copy.deepcopy(content) if content is not None else {}
            self.stamp[name] = datetime.now(UTC) - timedelta(seconds=60)
            return {}
        raise AssertionError(f"unexpected {method} {path}")


def nb(*sources: str) -> dict[str, Any]:
    cells = [
        {"id": f"c{i}", "cell_type": "code", "metadata": {}, "source": s, "outputs": [],
         "execution_count": None}
        for i, s in enumerate(sources)
    ]  # fmt: skip
    return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


@pytest.fixture
def server(home: Path, monkeypatch: pytest.MonkeyPatch) -> Contents:
    contents = Contents()
    jupyter.runner_dir("r").mkdir(parents=True)
    jupyter._write_state(jupyter.RunnerState("r", os.getpid(), 1, str(home), 0.0))
    (jupyter.runner_dir("r") / "token").write_text("t")
    monkeypatch.setattr(jupyter, "_http", contents)
    monkeypatch.setattr(jupyter, "running", jupyter._read_state)
    return contents


def test_cells_lists_position_id_type_and_output(server: Contents) -> None:
    book = nb("a = 1", "print(a)")
    book["cells"][1]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": "1\n"}]
    book["cells"][1]["execution_count"] = 4
    server.put("n.ipynb", book)
    found = jupyter_cells.cells("r", "n.ipynb")
    assert [(c.index, c.id, c.source) for c in found] == [(0, "c0", "a = 1"), (1, "c1", "print(a)")]
    assert (found[1].execution_count, found[1].outputs, found[1].output_text) == (4, 1, "1\n")


def test_edit_by_id_and_by_position_keeps_the_rest(server: Contents) -> None:
    server.put("n.ipynb", nb("a", "b", "c"))
    jupyter_cells.edit("r", "n.ipynb", "c1", "B2")
    jupyter_cells.edit("r", "n.ipynb", "-1", "C2")
    sources = [c["source"] for c in server.files["n.ipynb"]["cells"]]
    assert sources == ["a", "B2", "C2"]
    with pytest.raises(jupyter.RunnerError, match="no cell 'zz'"):
        jupyter_cells.edit("r", "n.ipynb", "zz", "x")
    with pytest.raises(jupyter.RunnerError, match="no cell '9'"):
        jupyter_cells.edit("r", "n.ipynb", "9", "x")


def test_edit_can_change_the_cell_type_and_keep_its_id(server: Contents) -> None:
    server.put("n.ipynb", nb("# hi"))
    jupyter_cells.edit("r", "n.ipynb", "0", "# hi", cell_type="markdown")
    cell = server.files["n.ipynb"]["cells"][0]
    assert (cell["id"], cell["cell_type"], "outputs" in cell) == ("c0", "markdown", False)


def test_insert_positions_and_ids_and_a_missing_notebook(server: Contents) -> None:
    server.put("n.ipynb", nb("a", "b"))
    jupyter_cells.insert("r", "n.ipynb", "last")
    jupyter_cells.insert("r", "n.ipynb", "first", position=0)
    jupyter_cells.insert("r", "n.ipynb", "# t", position=2, cell_type="markdown")
    cells = server.files["n.ipynb"]["cells"]
    assert [c["source"] for c in cells] == ["first", "a", "# t", "b", "last"]
    assert len({c["id"] for c in cells}) == 5
    jupyter_cells.insert("r", "new/dir/x.ipynb", "1")  # made when missing
    assert [c["source"] for c in server.files["new/dir/x.ipynb"]["cells"]] == ["1"]
    with pytest.raises(jupyter.RunnerError, match="cell_type"):
        jupyter_cells.insert("r", "n.ipynb", "x", cell_type="sql")


def test_delete_returns_what_was_removed(server: Contents) -> None:
    server.put("n.ipynb", nb("a", "b"))
    gone = jupyter_cells.delete("r", "n.ipynb", "c0")
    assert gone.source == "a" and [c["source"] for c in server.files["n.ipynb"]["cells"]] == ["b"]


def test_run_cell_stores_outputs_and_the_count(
    server: Contents, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.put("n.ipynb", nb("print(1)"))
    ran: list[str] = []
    result = jupyter.RunResult(
        "ok", "1\n", execution_count=3,
        outputs=({"output_type": "stream", "name": "stdout", "text": "1\n"},),
    )  # fmt: skip
    monkeypatch.setattr(
        jupyter, "run", lambda name, code, timeout, notebook: ran.append(code) or result
    )
    got, view = jupyter_cells.run_cell("r", "n.ipynb", "0")
    assert ran == ["print(1)"] and got.status == "ok"
    cell = server.files["n.ipynb"]["cells"][0]
    assert cell["outputs"] == list(result.outputs) and cell["execution_count"] == 3
    assert (view.outputs, view.execution_count) == (1, 3)


def test_append_run_adds_a_cell_with_the_code_and_outputs(server: Contents) -> None:
    server.put("n.ipynb", nb("a"))
    result = jupyter.RunResult("ok", "", execution_count=2, outputs=({"output_type": "stream"},))
    view = jupyter_cells.append_run("r", "n.ipynb", "b = 2", result)
    cell = server.files["n.ipynb"]["cells"][-1]
    assert (view.index, cell["source"], cell["execution_count"]) == (1, "b = 2", 2)


def test_an_edit_waits_while_the_user_is_typing_then_refuses(
    server: Contents, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.put("n.ipynb", nb("a"), age=0.0)  # saved just now
    monkeypatch.setattr(jupyter_cells, "SETTLE_SECONDS", 0.5)
    with pytest.raises(jupyter.RunnerError, match="someone is editing"):
        jupyter_cells.edit("r", "n.ipynb", "0", "x")
    assert server.files["n.ipynb"]["cells"][0]["source"] == "a"  # nothing was written
    monkeypatch.setattr(jupyter_cells, "QUIET_SECONDS", 0.3)
    monkeypatch.setattr(jupyter_cells, "SETTLE_SECONDS", 3.0)
    server.put("n.ipynb", nb("a"), age=0.0)
    started = time.monotonic()
    jupyter_cells.edit("r", "n.ipynb", "0", "x")  # waits about 0.3 s, then writes
    assert time.monotonic() - started >= 0.2
    assert server.files["n.ipynb"]["cells"][0]["source"] == "x"


def test_an_edit_refuses_when_the_file_changed_during_it(server: Contents) -> None:
    server.put("n.ipynb", nb("a"))
    calls = {"n": 0}

    def save_elsewhere(contents: Contents) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # right after the read, before the write check
            contents.stamp["n.ipynb"] = datetime.now(UTC) - timedelta(seconds=30)

    server.on_get = save_elsewhere
    with pytest.raises(jupyter.RunnerError, match="changed while it was being edited"):
        jupyter_cells.edit("r", "n.ipynb", "0", "x")
    assert server.files["n.ipynb"]["cells"][0]["source"] == "a"


def test_the_runner_command_installs_collaboration_unless_turned_off(tmp_path: Path) -> None:
    on = jupyter.command(jupyter.RunnerSpec("a", tmp_path), 1)
    off = jupyter.command(jupyter.RunnerSpec("a", tmp_path, collaboration=False), 1)
    assert "jupyter-collaboration" in on and "jupyter-collaboration" not in off


def test_the_guard_denies_writes_to_a_notebook_a_live_runner_serves(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    book = tmp_path / "sub" / "a.ipynb"
    monkeypatch.setattr(jupyter, "live_roots", lambda: [("lab", tmp_path)])
    reason = guard.deny_notebook_write({"file_path": str(book)})
    assert reason is not None and "jupyter_edit_cell" in reason and "'sub/a.ipynb'" in reason
    assert guard.deny_notebook_write({"notebook_path": str(book)}) == reason
    assert guard.deny_notebook_write({"file_path": str(tmp_path / "a.py")}) is None
    assert guard.deny_notebook_write({"file_path": "/elsewhere/a.ipynb"}) is None
    (home / "config.toml").write_text("[jupyter]\nguard = false\n")
    assert guard.deny_notebook_write({"file_path": str(book)}) is None


def test_live_roots_lists_runners_whose_process_is_alive(home: Path) -> None:
    for name, pid in (("up", os.getpid()), ("down", 2_000_000_000)):
        jupyter.runner_dir(name).mkdir(parents=True)
        jupyter._write_state(jupyter.RunnerState(name, pid, 1, f"/work/{name}", 0.0))
    assert jupyter.live_roots() == [("up", Path("/work/up"))]
