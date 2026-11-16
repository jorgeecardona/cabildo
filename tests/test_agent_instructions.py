"""The agent reads the nearest AGENTS.md, like other coding agents."""

from pathlib import Path

from cabildo.agent.instructions import MAX_CHARS, find_agents_md, with_instructions


def test_nearest_file_wins_over_the_root_one(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("pkg")
    assert find_agents_md(sub, tmp_path) == sub / "AGENTS.md"
    assert find_agents_md(tmp_path, tmp_path) == tmp_path / "AGENTS.md"


def test_walks_up_to_the_root_and_no_further(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("outside")
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    assert find_agents_md(root / "src", root) is None


def test_prompt_gets_the_text_and_long_files_are_cut(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * (MAX_CHARS + 50))
    prompt, used = with_instructions("SYS", tmp_path, tmp_path)
    assert used == tmp_path / "AGENTS.md"
    assert prompt.startswith("SYS\n\nProject instructions (AGENTS.md):")
    assert prompt.endswith("[truncated]")


def test_no_file_leaves_the_prompt_alone(tmp_path: Path) -> None:
    assert with_instructions("SYS", tmp_path, tmp_path) == ("SYS", None)
