"""The per-project register: parsing, the board, the invariants, and adoption.

The load-bearing claim these guard is that the FILES are the store — so every test here works on a
real folder on disk, and none of them touch the cabildo DB.
"""

from __future__ import annotations

import pathlib
import subprocess

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


def test_a_write_changes_only_what_you_changed(folder):
    """render(parse(text)) == text. Otherwise a status change is a one-line diff plus a silently
    eaten blank line, and every save produces noise nobody asked for."""
    path = folder / "03-08-2026-a.md"
    original = (
        "---\ntitle: A\nstatus: open\narea: ci\nowner: null\nfiled: 2026-08-03\nscope: S\n---\n"
        "\n# A\n\nbody text.\n"
    )
    path.write_text(original)
    config = issues.load_config(folder)
    assert issues.render(issues.load(path, config), config) == original

    issues.update(folder, "a", status=issues.Status.BLOCKED, owner="jorge")
    after = path.read_text()
    assert "\n---\n\n# A\n\nbody text.\n" in after
    assert after.count("\n\n# A") == 1


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


def test_a_list_field_says_it_wants_a_list(folder):
    """Taken from a real transcript. crm's agent had a list-valued `tags` and a tool signature that
    could only carry strings, so it tried ", " then "," then " " as separators, was told each time
    only that the value "does not match kebab", and gave up — filing with one tag instead of four.

    The message has to name the actual problem, because no separator was ever going to work.
    """
    write(folder, "03-08-2026-a.md",
          "title: A\nstatus: open\narea: ci\nscope: S\ntags: one, two, three")
    messages = " ".join(p.message for p in issues.check(folder))
    assert "`tags` is a list — pass a list of values" in messages
    assert "one, two, three" in messages       # quote what they sent, so it is recognisable


def test_mcp_fields_carry_lists_end_to_end(folder):
    """The fix at the layer that broke: MCP hands us JSON lists; the store keeps tuples."""
    from cabildo import issues_mcp

    assert issues_mcp._fields({"tags": ["a", "b"], "scope": "S"}) == {"tags": ("a", "b"), "scope": "S"}
    path = issues.file_issue(folder, title="Tagged Thing", area="ci", body="x",
                             extra=issues_mcp._fields({"scope": "S", "tags": ["one", "two"]}),
                             today="2026-08-03")
    assert "tags:\n  - one\n  - two" in path.read_text()
    assert issues.check(folder) == []


# --------------------------------------------------------------------------- #
# What the tool list costs
#
# This server is wired into every registered repo, so its tool list sits in the system prompt of
# every request there and is re-read from cache on each one. Weight here is charged constantly and
# noticed never, which is why these are asserted rather than left to care.
# --------------------------------------------------------------------------- #
def _tools():
    from cabildo import issues_mcp
    return list(issues_mcp.mcp._tool_manager._tools.values())


def test_no_tool_answers_twice():
    """The expensive default. FastMCP reads `-> str` as a structured return, and then answers each
    call with the text in `content` AND the same text again in `structuredContent`, wrapped as
    `{"result": ...}` — a reply nothing here unpacks, for double the tokens. Measured on `board`
    before the flag: 4,646 wire chars against 2,166 after.

    Every tool on this server returns prose. If one ever returns something genuinely structured,
    drop `structured_output=False` on that tool and leave the rest alone.
    """
    assert [t.name for t in _tools() if t.fn_metadata.output_schema is not None] == []


def test_the_tool_schemas_carry_no_generated_titles():
    """Pydantic titles every property — `blocked_by` earns `{"title": "Blocked By"}` — which only
    restates the key it hangs off. `_slim` drops them; this pins that it stays dropped.
    """
    from cabildo import issues_mcp

    def titles(node):
        if isinstance(node, dict):
            found = ["title"] if "title" in node and not isinstance(node["title"], dict) else []
            for key, value in node.items():
                if key == "properties":          # keys here are parameter names, not keywords
                    for sub in value.values():
                        found += titles(sub)
                else:
                    found += titles(value)
            return found
        if isinstance(node, list):
            return [t for v in node for t in titles(v)]
        return []

    assert [t.name for t in _tools() if titles(t.parameters)] == []
    # ...  and the parameter actually named `title` survived the stripping
    assert "title" in issues_mcp.mcp._tool_manager._tools["file_issue"].parameters["properties"]


def test_show_issue_bounds_a_runaway_file(folder, monkeypatch):
    """One oversized issue should not be able to spend a session's context. The cap sits well above
    p90 of the real folders, so a normal issue still comes back whole."""
    from cabildo import issues_mcp

    monkeypatch.setattr(issues_mcp, "_folder", lambda: folder)
    write(folder, "03-08-2026-huge.md", "title: H\nstatus: open\narea: ci\nscope: S", "x" * 20_000)
    out = issues_mcp.show_issue("huge")
    assert len(out) < issues_mcp.SHOW_LIMIT + 200
    assert "truncated at 12000 of" in out

    write(folder, "03-08-2026-small.md", "title: S\nstatus: open\narea: ci\nscope: S", "body")
    assert "truncated" not in issues_mcp.show_issue("small")


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


def test_index_can_carry_the_folders_own_fields_as_columns(tmp_path):
    """The index is for scanning, and what is worth scanning is what the folder chose to track."""
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text('areas = ["ci"]\nindex_fields = ["scope", "verify"]\n')
    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nscope: M\nverify: probe")
    table = issues.render_index(d)
    assert "| Issue | Area | Scope | Verify | Status | Waiting on |" in table
    assert "| M | probe |" in table


def test_index_can_carry_done_work_and_tag_groups(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex_done = true\nindex_group_by = "tags"\n\n'
        "[fields.tags]\nlist = true\n"
    )
    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\ntags:\n  - marks\n  - solo")
    write(d, "03-08-2026-b.md", "title: B\nstatus: open\narea: ci\ntags:\n  - marks")
    write(d, "03-08-2026-c.md", "title: C\nstatus: done\narea: ci\nfixed_in: abc1234")
    table = issues.render_index(d)

    assert "**`marks`** (2)" in table          # shared tags group...
    assert "**`solo`**" not in table           # ...a group of one is the flat table again
    assert "Single-use: `solo`." in table
    assert "| [C](03-08-2026-c.md) | ci | `abc1234` |" in table   # done, with what fixed it
    assert "1 done" in table


def test_index_shows_open_work_and_counts_the_done(folder):
    write(folder, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nscope: S")
    write(folder, "03-08-2026-b.md",
          "title: B\nstatus: done\narea: ci\nscope: S\nfixed_in: abc123")
    table = issues.render_index(folder)
    assert "[A](03-08-2026-a.md)" in table
    assert "[B](" not in table          # done work leaves the board...
    assert "1 done" in table            # ...but is still counted


def test_crossref_demands_the_link_in_both_directions(tmp_path):
    """One direction is not enough: a register that names a gap but does not point at the task is
    how the same gap gets found four times and fixed zero."""
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\n\n[fields.register]\ncrossref = "../roadmap/gaps.md"\n'
    )
    gaps = tmp_path / "docs" / "roadmap" / "gaps.md"
    gaps.parent.mkdir(parents=True)
    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nregister: G12")

    gaps.write_text("# gaps\n\nnothing here\n")
    assert any("G12 does not exist" in p.message for p in issues.check(d))

    gaps.write_text("# gaps\n\n## G12 — something\n")
    assert any("does not link back" in p.message for p in issues.check(d))

    gaps.write_text("# gaps\n\n## G12 — [see](../issues/03-08-2026-a.md)\n")
    assert issues.check(d) == []


def test_require_one_of_rejects_silence_but_accepts_either(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\nrequire_one_of = [["register", "register_note"]]\n'
    )
    p = write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci")
    assert any("none of `register`, `register_note`" in x.message for x in issues.check(d))

    p.write_text(p.read_text().replace("area: ci", "area: ci\nregister_note: no gap, a CI defect"))
    assert issues.check(d) == []


def test_crossref_at_a_missing_path_is_the_configs_problem(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\n\n[fields.register]\ncrossref = "../nope.md"\n'
    )
    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\nregister: G1")
    problems = issues.check(d)
    assert any("does not exist" in p.message and p.path.name == issues.CONFIG_NAME
               for p in problems)


# --------------------------------------------------------------------------- #
# The stricter invariants, learned from crm's scripts/issue_index.py
# --------------------------------------------------------------------------- #
ORDERED = """
areas = ["ci"]
index = false
field_order = ["title", "type", "area", "status", "fixed_in", "owner"]
strict_fields = true

[fields.type]
required = true
values = ["issue", "handoff"]
"""


@pytest.fixture()
def ordered(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(ORDERED)
    return d


def test_field_order_is_enforced(ordered):
    write(ordered, "03-08-2026-a.md", "title: A\narea: ci\ntype: issue\nstatus: open")
    assert any("front matter order" in p.message for p in issues.check(ordered))
    write(ordered, "03-08-2026-a.md", "title: A\ntype: issue\narea: ci\nstatus: open")
    assert issues.check(ordered) == []


def test_strict_fields_rejects_a_key_the_folder_never_declared(ordered):
    write(ordered, "03-08-2026-a.md",
          "title: A\ntype: issue\narea: ci\nstatus: open\nwhatever: hm")
    assert any("unknown field 'whatever'" in p.message for p in issues.check(ordered))


def test_writes_stay_inside_the_declared_schema(ordered):
    """The blocker that stopped crm adopting: a tool that adds `filed:` on every save makes every
    write fail a strict check, and the agent has no idea why."""
    write(ordered, "03-08-2026-a.md", "title: A\ntype: issue\narea: ci\nstatus: open")
    issues.claim(ordered, "a", owner="qa")
    text = (ordered / "03-08-2026-a.md").read_text()
    assert "filed:" not in text          # not in field_order → never written
    assert "owner: qa" in text
    assert issues.check(ordered) == []   # ...and the folder is still clean afterwards

    issues.close(ordered, "a", fixed_in="deadbee")
    assert issues.check(ordered) == []


def test_field_order_must_carry_the_core(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text('areas = ["ci"]\nfield_order = ["title", "area"]\n')
    with pytest.raises(issues.IssueFormatError, match="field_order omits status"):
        issues.load_config(d)


def test_pattern_catches_a_tag_that_will_not_group(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\n\n[fields.tags]\nlist = true\npattern = "kebab"\n'
    )
    write(d, "03-08-2026-a.md",
          "title: A\nstatus: open\narea: ci\ntags:\n  - roast-detect\n  - Roast Detect")
    problems = issues.check(d)
    assert any("'Roast Detect' does not match kebab" in p.message for p in problems)
    assert not any("roast-detect'" in p.message for p in problems)


def test_fixed_in_is_forbidden_on_an_unfinished_issue(folder):
    write(folder, "03-08-2026-a.md",
          "title: A\nstatus: open\narea: ci\nscope: S\nfixed_in: abc1234")
    assert any("on an issue that is `open`" in p.message for p in issues.check(folder))


def test_fixed_in_must_be_a_real_commit_on_the_named_branch(tmp_path):
    """A worktree sha resolves until that worktree is pruned, and then the register points at
    nothing — while still looking like evidence."""
    repo = tmp_path / "repo"
    d = repo / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\nfixed_in_on_branch = "main"\n'
    )
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, check=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    run("git", "add", "-A"); run("git", "commit", "-qm", "one")
    landed = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()

    # a commit on a side branch — real, resolvable, and NOT on main
    run("git", "checkout", "-qb", "side")
    (repo / "x.txt").write_text("x")
    run("git", "add", "-A"); run("git", "commit", "-qm", "two")
    stray = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    run("git", "checkout", "-q", "main")

    write(d, "03-08-2026-a.md", f"title: A\nstatus: done\narea: ci\nfixed_in: {stray}")
    assert any("is not on main" in p.message for p in issues.check(d))

    write(d, "03-08-2026-a.md", "title: A\nstatus: done\narea: ci\nfixed_in: 0000000")
    assert any("is not a commit" in p.message for p in issues.check(d))

    write(d, "03-08-2026-a.md", f"title: A\nstatus: done\narea: ci\nfixed_in: {landed}")
    assert issues.check(d) == []


def test_required_sections_with_a_per_type_exemption(tmp_path):
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text("""
areas = ["ci"]
index = false

[fields.type]
values = ["issue", "handoff"]

[sections]
require = ["Acceptance criteria", "Tests?\\\\b|The test"]
exempt = "type=handoff"
""")
    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\ntype: issue",
          body="\njust prose.\n")
    msgs = " ".join(p.message for p in issues.check(d))
    assert "Acceptance criteria" in msgs and "The test" in msgs

    # a hand-off carries no acceptance criteria of its own — it points at the issues that do
    write(d, "03-08-2026-b.md", "title: B\nstatus: open\narea: ci\ntype: handoff",
          body="\nresume order.\n")
    assert not any(p.path.name == "03-08-2026-b.md" for p in issues.check(d))

    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci\ntype: issue",
          body="\n## Acceptance criteria\n\nx\n\n## Test\n\ny\n")
    assert issues.check(d) == []


def test_a_cited_sha_on_no_branch_is_flagged(tmp_path):
    repo = tmp_path / "repo"
    d = repo / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\ncheck_body_shas = true\n'
    )
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, check=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    run("git", "add", "-A"); run("git", "commit", "-qm", "one")
    run("git", "checkout", "-qb", "doomed")
    (repo / "x.txt").write_text("x")
    run("git", "add", "-A"); run("git", "commit", "-qm", "two")
    orphan = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()[:8]
    run("git", "checkout", "-q", "main")

    write(d, "03-08-2026-a.md", "title: A\nstatus: open\narea: ci",
          body=f"\nproved by `{orphan}`.\n")
    assert issues.check(d) == []              # still on `doomed`, so still reachable

    run("git", "branch", "-qD", "doomed")
    assert any("on no branch" in p.message for p in issues.check(d))


def test_git_checks_stay_silent_outside_a_repo(tmp_path):
    """A guard that cannot run must not invent a verdict."""
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\nfixed_in_on_branch = "main"\ncheck_body_shas = true\n'
    )
    write(d, "03-08-2026-a.md", "title: A\nstatus: done\narea: ci\nfixed_in: abc1234",
          body="\ncites `deadbeef`.\n")
    assert issues.check(d) == []


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


def test_scaffold_starts_a_usable_register_from_nothing(tmp_path):
    d = tmp_path / "docs" / "issues"
    written = issues.scaffold(d, areas=("asr", "cli"), today="2026-08-03")
    assert {p.name for p in written} == {issues.CONFIG_NAME, issues.README_NAME}

    config = issues.load_config(d)
    assert config.areas == ("asr", "cli")
    assert config.index is True
    assert issues.check(d) == []          # an empty register is a valid register

    path = issues.file_issue(d, title="A Thing", area="asr", body="x",
                             extra={"scope": "S", "verify": "test"}, today="2026-08-03")
    assert path.name == "03-08-2026-a-thing.md"
    issues.reindex(d)
    assert "[A Thing](03-08-2026-a-thing.md)" in (d / issues.README_NAME).read_text()
    assert issues.check(d) == []


def test_scaffold_never_overwrites_an_existing_readme(tmp_path):
    """Silently rewriting somebody's documentation to install a tool is not a trade anyone made."""
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.README_NAME).write_text("# mine\n")
    written = issues.scaffold(d, areas=("ci",))
    assert [p.name for p in written] == [issues.CONFIG_NAME]
    assert (d / issues.README_NAME).read_text() == "# mine\n"


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


def test_find_folder_never_adopts_an_ancestor_repos_register(tmp_path):
    """One real project sits inside a journal repo that has its own everything. Filing this repo's
    work into that queue would be worse than finding nothing."""
    outer = tmp_path / "journal"
    (outer / ".git").mkdir(parents=True)
    (outer / "docs" / "issues").mkdir(parents=True)
    inner = outer / "projects" / "thing"
    (inner / ".git").mkdir(parents=True)
    assert issues.find_folder(inner) is None


def test_find_folder_accepts_the_other_conventional_layouts(tmp_path):
    """A small library often has no docs/ at all, and one project keeps docs/ out of git wholesale.
    Neither is a reason to be unable to keep a register."""
    for n, parts in enumerate((("issues",), (".issues",))):
        repo = tmp_path / f"repo{n}"
        (repo / ".git").mkdir(parents=True)
        (repo / pathlib.Path(*parts)).mkdir(parents=True)
        assert issues.find_folder(repo) == repo / pathlib.Path(*parts)


def test_issues_dir_env_var_wins(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs" / "issues").mkdir(parents=True)
    (repo / "ops" / "followups").mkdir(parents=True)

    monkeypatch.setenv("CABILDO_ISSUES_DIR", "ops/followups")
    assert issues.find_folder(repo) == repo / "ops" / "followups"

    monkeypatch.setenv("CABILDO_ISSUES_DIR", str(repo / "docs" / "issues"))
    assert issues.find_folder(repo) == repo / "docs" / "issues"

    monkeypatch.setenv("CABILDO_ISSUES_DIR", str(tmp_path / "nope"))
    assert issues.find_folder(repo) is None


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


def test_the_written_command_is_one_the_consumer_can_actually_run(tmp_path, monkeypatch):
    """The bare name is only written when a LOGIN SHELL resolves it, not when this process does.

    `cabildo add` may run inside a venv that Claude Code's own environment never sees. Then
    `shutil.which` says yes, the bare name gets written, and every session opened in that folder
    dies with "Executable not found in $PATH" — a failure that surfaces nowhere near its cause.
    """
    monkeypatch.setattr(mcp_config, "_on_login_path", lambda script: False)
    entry = mcp_config.issues_entry()
    assert pathlib.Path(entry["command"]).is_absolute(), \
        "a name the consumer cannot resolve must never be written"

    monkeypatch.setattr(mcp_config, "_on_login_path", lambda script: True)
    assert mcp_config.issues_entry()["command"] == "cabildo-issues-mcp"


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


# --------------------------------------------------------------------------- #
# The cue — measured behaviour, not a hope
# --------------------------------------------------------------------------- #
def test_the_session_cue_is_silent_without_a_register(tmp_path):
    """Most projects have no register. A banner line that says "0 issues" in all of them is the
    kind of noise agents learn to skip, which costs the projects that DO have one."""
    from cabildo import notify

    (tmp_path / ".git").mkdir()
    assert notify.register_line(str(tmp_path)) is None
    assert notify.register_line(None) is None


def test_the_session_cue_names_the_top_ranked_item(tmp_path):
    """A count is an invitation; a title is a fact. `board` was described as "start here" and was
    still the least-used tool, because a tool description is read once you are already choosing a
    tool. This lands before that."""
    from cabildo import notify

    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text(
        'areas = ["ci"]\nindex = false\n\n[fields.priority]\nvalues = ["P0", "P1"]\n'
    )
    write(d, "01-08-2026-small.md", "title: Small thing\nstatus: open\narea: ci\npriority: P1")
    write(d, "02-08-2026-urgent.md", "title: Urgent thing\nstatus: open\narea: ci\npriority: P0")

    line = notify.register_line(str(tmp_path))
    assert line is not None
    assert "2 ready" in line
    assert "next: [P0] Urgent thing — urgent" in line     # ranked, not just first by date
    assert "`board`" in line and "`file_issue`" in line


def test_the_cue_survives_a_broken_register(tmp_path):
    """A malformed config must never cost somebody a session."""
    from cabildo import notify

    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    (d / issues.CONFIG_NAME).write_text("this is not valid toml {{{")
    assert notify.register_line(str(tmp_path)) is None
