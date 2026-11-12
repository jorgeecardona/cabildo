"""``next_issue`` wraps prodc's ranking and reports a skip reason when there is no work."""

import pathlib

from cabildo import issues_mcp

_REAL_NEED = min((pathlib.Path(__file__).parent.parent / "product" / "needs").glob("*.yaml"))


def test_no_prodc_board_skips(tmp_path, monkeypatch):
    """A repo without prodc.toml reports the no-board skip."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(issues_mcp, "_identity", lambda: (None, None))
    assert "skip: no-board" in issues_mcp.next_issue()


def test_board_without_releases_skips(tmp_path, monkeypatch):
    """A prodc board with needs but no releases reports no-releases."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "prodc.toml").write_text('[prodc]\nneeds = ["product/needs/*.yaml"]\n')
    needs = tmp_path / "product" / "needs"
    needs.mkdir(parents=True)
    (needs / "a.yaml").write_text(_REAL_NEED.read_text())
    (tmp_path / "docs" / "issues").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(issues_mcp, "_identity", lambda: (None, None))
    out = issues_mcp.next_issue()
    assert "skip:" in out, out
