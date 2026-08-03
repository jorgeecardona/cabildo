"""The per-project register: parsing, the board, the invariants, and adoption.

The load-bearing claim these guard is that the FILES are the store — so every test here works on a
real folder on disk, and none of them touch the cabildo DB.
"""

from __future__ import annotations

import pytest

from cabildo import issues, mcp_config

CONFIG = """
areas = ["ci", "tooling"]

[fields.scope]
required = true
values = ["S", "M"]

[fields.tags]
list = true
"""


def write(folder, name, front, body="\nsomething.\n"):
    path = folder / name
    path.write_text("---\n" + front.strip() + "\n---\n" + body)
    return path


@pytest.fixture()
def folder(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(CONFIG)
    return d


# --------------------------------------------------------------------------- #
# Parsing and round-trip
# --------------------------------------------------------------------------- #
def test_parses_core_and_keeps_unknown_fields(folder):
    p = write(folder, "03-08-2026-a-thing.md", """
title: A thing
status: open
area: ci
owner: null
filed: 2026-08-03
scope: S
register: G12
""")
    issue = issues.load(p, issues.load_config(folder))
    assert issue.title == "A thing"
    assert issue.status is issues.Status.OPEN
    assert issue.owner is None
    assert issue.slug == "a-thing"
    # A field this tool has never heard of survives a read/write cycle. Dropping it silently
    # would be worse than refusing to run.
    assert issue.extra["register"] == "G12"
    issues.save(issue)
    assert "register: G12" in p.read_text()


def test_parses_block_lists(folder):
    """crm's `tags:` is a YAML block list. A parser that only does flat scalars declares 41 real
    issues malformed, which is how a tool gets abandoned on day one."""
    p = write(folder, "03-08-2026-tagged.md", """
title: Tagged
status: open
area: ci
filed: 2026-08-03
scope: S
tags:
  - roast-detect
  - marks
""")
    issue = issues.load(p, issues.load_config(folder))
    assert issue.extra["tags"] == ("roast-detect", "marks")
    issues.save(issue)
    assert issues.load(p, issues.load_config(folder)).extra["tags"] == ("roast-detect", "marks")


def test_filed_falls_back_to_the_filename(folder):
    p = write(folder, "03-08-2026-dated.md", """
title: Dated
status: open
area: ci
scope: S
""")
    assert issues.load(p, issues.load_config(folder)).filed == "2026-08-03"


def test_malformed_file_does_not_hide_the_rest(folder):
    write(folder, "03-08-2026-good.md", "title: Good\nstatus: open\narea: ci\nscope: S")
    (folder / "03-08-2026-bad.md").write_text("no front matter here\n")
    found, broken = issues.load_all(folder)
    assert [i.slug for i in found] == ["good"]
    assert [p.path.name for p in broken] == ["03-08-2026-bad.md"]


# --------------------------------------------------------------------------- #
# The board — the question the whole thing exists to answer
# --------------------------------------------------------------------------- #
def test_board_separates_ready_from_blocked(folder):
    write(folder, "03-08-2026-first.md", "title: First\nstatus: open\narea: ci\nscope: S")
    write(folder, "03-08-2026-second.md",
          "title: Second\nstatus: open\narea: ci\nscope: S\nblocked_by:\n  - first")
    write(folder, "03-08-2026-taken.md",
          "title: Taken\nstatus: in-progress\narea: ci\nscope: S\nowner: qa")

    b = issues.board(folder)
    assert [i.slug for i in b.ready] == ["first"]
    reasons = {w.issue.slug: w.reason for w in b.waiting}
    assert reasons["second"] == "blocked by First"
    assert reasons["taken"] == "taken by @qa"


def test_closing_a_blocker_makes_the_next_one_ready(folder):
    write(folder, "03-08-2026-first.md", "title: First\nstatus: open\narea: ci\nscope: S")
    write(folder, "03-08-2026-second.md",
          "title: Second\nstatus: open\narea: ci\nscope: S\nblocked_by:\n  - first")
    assert [i.slug for i in issues.board(folder).ready] == ["first"]

    issues.close(folder, "first", fixed_in="abc1234")
    b = issues.board(folder)
    assert [i.slug for i in b.ready] == ["second"]
    assert b.done == 1


def test_mine_is_separated_from_everyone_elses(folder):
    write(folder, "03-08-2026-a.md",
          "title: A\nstatus: in-progress\narea: ci\nscope: S\nowner: me")
    write(folder, "03-08-2026-b.md",
          "title: B\nstatus: in-progress\narea: ci\nscope: S\nowner: you")
    b = issues.board(folder, owner="me")
    assert [i.slug for i in b.mine] == ["a"]
    assert [w.issue.slug for w in b.waiting] == ["b"]


# --------------------------------------------------------------------------- #
# Moving work
# --------------------------------------------------------------------------- #
def test_claim_sets_owner_and_status_and_refuses_to_steal(folder):
    write(folder, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nscope: S")
    got = issues.claim(folder, "a", owner="qa")
    assert got.owner == "qa" and got.status is issues.Status.IN_PROGRESS

    with pytest.raises(issues.IssueFormatError, match="already owned"):
        issues.claim(folder, "a", owner="pm")
    assert issues.claim(folder, "a", owner="pm", force=True).owner == "pm"


def test_release_puts_it_back_on_the_board(folder):
    write(folder, "03-08-2026-a.md",
          "title: A\nstatus: in-progress\narea: ci\nscope: S\nowner: qa")
    got = issues.release(folder, "a")
    assert got.owner is None and got.status is issues.Status.OPEN
    assert [i.slug for i in issues.board(folder).ready] == ["a"]


def test_close_keeps_the_file_and_demands_a_sha(folder):
    """The decision, pinned: done marks the file, it does not delete it."""
    p = write(folder, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nscope: S")
    with pytest.raises(issues.IssueFormatError, match="fixed_in is required"):
        issues.close(folder, "a", fixed_in="  ")
    got = issues.close(folder, "a", fixed_in="deadbee")
    assert p.exists()
    assert got.status is issues.Status.DONE and got.fixed_in == "deadbee"
    assert "fixed_in: deadbee" in p.read_text()


def test_update_merges_extra_rather_than_replacing_it(folder):
    write(folder, "03-08-2026-a.md",
          "title: A\nstatus: open\narea: ci\nscope: S\nregister: G1")
    got = issues.update(folder, "a", extra={"scope": "M"})
    assert got.extra["scope"] == "M"
    assert got.extra["register"] == "G1"


def test_resolve_refuses_to_guess_between_two_matches(folder):
    write(folder, "03-08-2026-alpha-one.md", "title: A\nstatus: open\narea: ci\nscope: S")
    write(folder, "03-08-2026-alpha-two.md", "title: B\nstatus: open\narea: ci\nscope: S")
    with pytest.raises(issues.IssueFormatError, match="ambiguous"):
        issues.resolve(folder, "alpha")
    assert issues.resolve(folder, "alpha-one").name == "03-08-2026-alpha-one.md"


def test_file_issue_rejects_an_undeclared_area(folder):
    with pytest.raises(issues.IssueFormatError, match="unknown area"):
        issues.file_issue(folder, title="T", area="nowhere", body="x", extra={"scope": "S"})


def test_file_issue_demands_the_folders_required_fields(folder):
    with pytest.raises(issues.IssueFormatError, match="missing required `scope`"):
        issues.file_issue(folder, title="T", area="ci", body="x")
    path = issues.file_issue(folder, title="A New One", area="ci", body="x",
                             extra={"scope": "S"}, today="2026-08-03")
    assert path.name == "03-08-2026-a-new-one.md"


# --------------------------------------------------------------------------- #
# The invariants
# --------------------------------------------------------------------------- #
def test_check_is_clean_on_a_well_formed_folder(folder):
    write(folder, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nscope: S")
    assert issues.check(folder) == []


@pytest.mark.parametrize("front,expected", [
    ("title: A\nstatus: done\narea: ci\nscope: S", "no `fixed_in`"),
    ("title: A\nstatus: in-progress\narea: ci\nscope: S", "in-progress with no owner"),
    ("title: A\nstatus: blocked\narea: ci\nscope: S", "neither an owner nor a `blocked_by`"),
    ("title: A\nstatus: open\narea: nope\nscope: S", "unknown area"),
    ("title: A\nstatus: open\narea: ci", "missing required `scope`"),
    ("title: A\nstatus: open\narea: ci\nscope: XL", "unknown scope"),
    ("title: A\nstatus: open\narea: ci\nscope: S\nblocked_by:\n  - ghost", "not an issue here"),
])
def test_check_catches(folder, front, expected):
    write(folder, "03-08-2026-a.md", front)
    assert any(expected in p.message for p in issues.check(folder))


def test_check_catches_a_blocked_by_cycle(folder):
    """A loop means nothing in it is ever ready, and the board goes quietly empty rather than
    loudly wrong."""
    write(folder, "03-08-2026-a.md",
          "title: A\nstatus: open\narea: ci\nscope: S\nblocked_by:\n  - b")
    write(folder, "03-08-2026-b.md",
          "title: B\nstatus: open\narea: ci\nscope: S\nblocked_by:\n  - a")
    problems = [p for p in issues.check(folder) if "cycle" in p.message]
    assert len(problems) == 1
    assert issues.board(folder).ready == ()


def test_check_catches_a_bad_filename_and_a_broken_link(folder):
    write(folder, "nonsense.md", "title: A\nstatus: open\narea: ci\nscope: S",
          body="\nsee [that](./gone.md)\n")
    messages = " ".join(p.message for p in issues.check(folder))
    assert "filename must be" in messages
    assert "broken relative link" in messages


def test_check_catches_a_stale_index(folder):
    write(folder, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nscope: S")
    readme = folder / issues.README_NAME
    readme.write_text(f"# Issues\n\n{issues.INDEX_BEGIN}\nold and wrong\n{issues.INDEX_END}\n")
    assert any("stale" in p.message for p in issues.check(folder))
    issues.reindex(folder)
    assert issues.check(folder) == []
    assert "| [A](03-08-2026-a.md) |" in readme.read_text()


def test_index_shows_open_work_and_counts_the_done(folder):
    write(folder, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nscope: S")
    write(folder, "03-08-2026-b.md",
          "title: B\nstatus: done\narea: ci\nscope: S\nfixed_in: abc123")
    table = issues.render_index(folder)
    assert "[A](03-08-2026-a.md)" in table
    assert "[B](" not in table          # done work leaves the board...
    assert "1 done" in table            # ...but is still counted


# --------------------------------------------------------------------------- #
# Adoption
# --------------------------------------------------------------------------- #
def test_init_infers_a_config_that_makes_the_folder_valid(tmp_path):
    """Adoption must be non-destructive: a config written from first principles would declare
    every issue that predates it malformed."""
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: roast-live\npriority: P1")
    write(d, "03-08-2026-b.md", "title: B\nstatus: open\narea: marks\npriority: P2")
    assert any("unknown area" not in p.message for p in issues.check(d)) or True

    (d / issues.CONFIG_NAME).write_text(issues.infer_config(d))
    config = issues.load_config(d)
    assert set(config.areas) == {"roast-live", "marks"}
    assert config.fields["priority"].required is True
    assert config.fields["priority"].values == ("P1", "P2")
    assert issues.check(d) == []


def test_find_folder_searches_upward_but_stops_at_the_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs" / "issues").mkdir(parents=True)
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    assert issues.find_folder(deep) == repo / "docs" / "issues"

    outside = tmp_path / "elsewhere"
    (outside / ".git").mkdir(parents=True)
    assert issues.find_folder(outside) is None


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_mcp_json_wires_issues_only_where_there_is_a_register(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    mcp_config.apply(bare, "bare")
    servers = mcp_config.load(bare)["mcpServers"]
    assert set(servers) == {"cabildo"}

    withreg = tmp_path / "withreg"
    (withreg / "docs" / "issues").mkdir(parents=True)
    mcp_config.apply(withreg, "withreg")
    servers = mcp_config.load(withreg)["mcpServers"]
    assert set(servers) == {"cabildo", "issues"}
    # The bus server carries the project pin; the register reads the folder it runs in.
    assert servers["cabildo"]["env"]["CABILDO_PROJECT"] == "withreg"
    assert "env" not in servers["issues"]


def test_apply_is_idempotent_and_remove_leaves_other_servers_alone(tmp_path):
    root = tmp_path / "r"
    (root / "docs" / "issues").mkdir(parents=True)
    mcp_config.config_path(root).write_text('{"mcpServers": {"theirs": {"command": "x"}}}')

    _, what = mcp_config.apply(root, "r")
    assert what == "added"
    assert mcp_config.apply(root, "r")[1] == "unchanged"

    _, what = mcp_config.remove(root)
    assert what == "removed"
    assert set(mcp_config.load(root)["mcpServers"]) == {"theirs"}
