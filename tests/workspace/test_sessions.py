"""sessions.py: transcript parsing, continued-in chains, rows, sidecar."""

from __future__ import annotations

import json

import pytest
from lines import (
    PROJECT,
    ai_title,
    assistant,
    claude_assistant,
    claude_user,
    continued_in,
    custom_title,
    reply,
    user,
)

from cabildo.workspace import sessions
from cabildo.workspace.sessions import Filter, Meta, Tokens


def test_encode_cwd_replaces_every_non_alphanumeric() -> None:
    assert sessions.encode_cwd("/home/a.b/c_d") == "-home-a-b-c-d"


def test_parse_reads_cwd_branch_titles_and_first_real_prompt(transcript) -> None:
    path = transcript(
        "s1",
        user("<command-name>/clear</command-name>"),  # noise: skipped for the preview
        user("/model opus"),  # slash command: skipped too
        user("Fix the login bug", branch="feat/login"),
        assistant("On it."),
        {"type": "attachment", "cwd": f"{PROJECT}/web"},  # Claude cd'd into a subdir
        ai_title("Login bug"),
        custom_title("login"),
    )
    s = sessions.parse_transcript(path)
    assert s is not None
    assert (s.session_id, s.cwd, s.last_cwd) == ("s1", PROJECT, f"{PROJECT}/web")
    assert s.git_branch == "main"  # first user line wins
    assert s.first_prompt == "Fix the login bug"
    assert (s.ai_title, s.custom_title) == ("Login bug", "login")
    assert s.has_messages and s.continued_in == ""


def test_parse_continuation_stub_has_no_messages(transcript) -> None:
    path = transcript(
        "c1", ai_title("reply-with-alpha"), {"type": "agent-name", "agentName": "reply-with-alpha"}
    )
    s = sessions.parse_transcript(path)
    assert s is not None
    assert not s.has_messages
    assert s.ai_title == "reply-with-alpha"


def test_parse_reads_continued_in_and_skips_broken_lines(transcript) -> None:
    path = transcript("a1", user("hello"), continued_in("c1"))
    path.write_text(path.read_text() + "{not json\n\n")
    s = sessions.parse_transcript(path)
    assert s is not None
    assert s.continued_in == "c1"


def _sessions(transcript, specs: dict[str, str]) -> list[sessions.Session]:
    """specs: id -> id it continued in ('' for none)."""
    out = []
    for sid, nxt in specs.items():
        lines = [user(f"prompt {sid}")] + ([continued_in(nxt)] if nxt else [])
        s = sessions.parse_transcript(transcript(sid, *lines))
        assert s is not None
        out.append(s)
    return out


def _ids(chains: list[tuple[sessions.Session, ...]]) -> list[list[str]]:
    return sorted([s.session_id for s in c] for c in chains)


def test_chains_follow_continued_in(transcript) -> None:
    ss = _sessions(transcript, {"a": "b", "b": "c", "c": "", "x": ""})
    assert _ids(sessions.chains(ss)) == [["a", "b", "c"], ["x"]]


def test_chains_ignore_a_link_to_a_missing_transcript(transcript) -> None:
    ss = _sessions(transcript, {"a": "gone"})
    assert _ids(sessions.chains(ss)) == [["a"]]


def test_chains_keep_a_cyclic_chain_instead_of_dropping_it(transcript) -> None:
    ss = _sessions(transcript, {"a": "b", "b": "a"})
    chains = sessions.chains(ss)
    assert len(chains) == 1
    assert sorted(s.session_id for s in chains[0]) == ["a", "b"]


def _row(transcript, *specs: tuple[str, list[dict]]) -> sessions.Row:
    chain = []
    for sid, lines in specs:
        s = sessions.parse_transcript(transcript(sid, *lines))
        assert s is not None
        chain.append(s)
    return sessions.Row(chain=tuple(chain), meta=Meta(), meta_key=chain[0].session_id)


def test_row_title_prefers_custom_then_ai_then_origin_prompt(transcript) -> None:
    only_prompt = _row(transcript, ("p", [user("first question")]))
    assert only_prompt.title == "first question"
    with_ai = _row(transcript, ("a", [user("q"), ai_title("old")]), ("b", [ai_title("new")]))
    assert with_ai.title == "new"  # newest ai-title in the chain
    with_custom = _row(
        transcript, ("c", [user("q"), custom_title("mine")]), ("d", [ai_title("ai")])
    )
    assert with_custom.title == "mine"  # a custom title beats any ai-title


def test_row_head_cwd_and_resumable(transcript) -> None:
    r = _row(
        transcript, ("origin", [user("q", cwd=PROJECT), assistant("a")]), ("stub", [ai_title("t")])
    )
    assert r.head.session_id == "stub"
    assert r.ids == ("origin", "stub")
    assert r.cwd == PROJECT  # stub has no cwd: falls back
    assert r.resumable.session_id == "origin"  # --resume cannot load a stub


def test_build_rows_merges_chains_and_sorts_by_group_then_newest(transcript) -> None:
    transcript("old", user("old one"), mtime=1_000)
    transcript("mid", user("mid one"), continued_in("mid2"), mtime=2_000)
    transcript("mid2", ai_title("mid continued"), mtime=5_000)
    transcript("grouped", user("grouped one"), mtime=500)
    sessions.save_meta({"grouped": Meta(group="work")})
    rows = sessions.build_rows(PROJECT)
    assert [r.ids for r in rows] == [("grouped",), ("mid", "mid2"), ("old",)]
    assert rows[1].mtime == 5_000  # newest file of the chain


def test_meta_key_is_the_chain_id_that_already_has_metadata(transcript) -> None:
    transcript("a", user("q"), continued_in("b"))
    transcript("b", ai_title("t"))
    assert sessions.build_rows(PROJECT)[0].meta_key == "a"  # nothing saved: the origin
    sessions.save_meta({"b": Meta(tags=("x",))})
    row = sessions.build_rows(PROJECT)[0]
    assert (row.meta_key, row.meta.tags) == ("b", ("x",))


def test_filter_is_or_inside_and_across_without_case() -> None:
    only = Filter(groups=("work", "infra"), tags=("urgent",))
    assert only.matches(Meta(group="Work", tags=("x", "URGENT")))
    assert only.matches(Meta(group="infra", tags=("urgent",)))
    assert not only.matches(Meta(group="work"))  # no tag
    assert not only.matches(Meta(group="home", tags=("urgent",)))  # other group
    assert not only.matches(Meta(tags=("urgent",)))  # no group
    assert Filter(tags=("a", "b")).matches(Meta(tags=("b",)))
    assert Filter().matches(Meta()) and not Filter().active


def test_filter_text_and_parse_are_inverse() -> None:
    only = Filter(groups=("work", "my team"), tags=("urgent",))
    assert only.text() == "g:work 'g:my team' t:urgent"
    assert Filter.parse(only.text()) == only
    assert Filter.parse("  ") == Filter()
    assert Filter.parse("G:work T:x") == Filter(groups=("work",), tags=("x",))


@pytest.mark.parametrize("text", ["work", "x:work", "g:", "t: "])
def test_filter_parse_refuses_words_without_g_or_t(text: str) -> None:
    with pytest.raises(ValueError):
        Filter.parse(text)


def test_build_rows_keeps_only_what_the_filter_matches(transcript) -> None:
    for sid in ["a", "b", "c"]:
        transcript(sid, user(sid))
    sessions.save_meta({"a": Meta(group="work"), "b": Meta(group="work", tags=("urgent",))})
    rows = sessions.build_rows(PROJECT, only=Filter(groups=("work",)))
    assert sorted(r.head.session_id for r in rows) == ["a", "b"]
    rows = sessions.build_rows(PROJECT, only=Filter(tags=("urgent",)))
    assert [r.head.session_id for r in rows] == ["b"]


def test_sidecar_roundtrip_drops_empty_entries(claude_home) -> None:
    sessions.save_meta({"a": Meta(label="L", group="G", tags=("t1", "t2")), "b": Meta()})
    assert sessions.load_meta() == {"a": Meta(label="L", group="G", tags=("t1", "t2"))}


def test_build_rows_of_an_unknown_dir_is_empty(claude_home) -> None:
    assert sessions.build_rows("/nowhere") == []


def test_trash_moves_every_transcript_and_records_where_from(transcript) -> None:
    a = transcript("a", user("q"), continued_in("b"))
    b = transcript("b", ai_title("t"))
    sessions.save_meta({"b": Meta(label="keep me?"), "other": Meta(label="stay")})
    (row,) = sessions.build_rows(PROJECT)
    folder = sessions.trash(row)
    assert not a.exists() and not b.exists()
    assert (folder / "a.jsonl").exists() and (folder / "b.jsonl").exists()
    restore = (folder / "RESTORE.txt").read_text()
    assert f"{folder / 'a.jsonl'} -> {a}" in restore
    assert sessions.load_meta() == {"other": Meta(label="stay")}
    assert sessions.build_rows(PROJECT) == []


def test_scan_reads_only_what_changed(transcript, monkeypatch) -> None:
    a = transcript("a", user("one"))
    transcript("b", user("two"))
    first_size = a.stat().st_size
    reads: list[tuple[str, int]] = []  # (file, where the read started)
    real = sessions.read_transcript

    def spy(p, prev=None):
        reads.append((p.stem, prev.end if prev else 0))
        return real(p, prev)

    monkeypatch.setattr(sessions, "read_transcript", spy)
    sessions.scan(PROJECT)
    sessions.scan(PROJECT)  # nothing changed: no reads
    with a.open("a") as fh:  # Claude appends a line
        fh.write(json.dumps({"sessionId": "a", **ai_title("new")}) + "\n")
    rows = {s.session_id: s for s in sessions.scan(PROJECT)}
    assert sorted(reads) == [("a", 0), ("a", first_size), ("b", 0)]  # only the new bytes
    assert rows["a"].ai_title == "new" and rows["a"].first_prompt == "one"


def test_appended_lines_read_like_a_full_read(transcript) -> None:
    lines = [
        user("Fix it", branch="dev"),
        assistant("ok"),
        ai_title("T1"),
        user("more", cwd=f"{PROJECT}/sub"),
        custom_title("mine"),
        continued_in("c"),
    ]
    path = transcript("inc")
    read = None
    for i, line in enumerate(lines):
        with path.open("a") as fh:
            fh.write(json.dumps({"sessionId": "inc", **line}) + "\n")
        read = sessions.read_transcript(path, read)
        assert read is not None and read.end == path.stat().st_size, i
    assert read is not None and read.session == sessions.parse_transcript(path)


def test_a_line_still_being_written_is_read_again(transcript) -> None:
    path = transcript("w", user("hi"))
    whole = json.dumps({"sessionId": "w", **ai_title("Done title")}) + "\n"
    with path.open("a") as fh:
        fh.write(whole[:20])  # Claude is mid-write
    read = sessions.read_transcript(path)
    assert read is not None and read.session.ai_title == ""
    assert read.end == path.stat().st_size - 20  # stops before the cut line
    with path.open("a") as fh:
        fh.write(whole[20:])
    again = sessions.read_transcript(path, read)
    assert again is not None and again.session.ai_title == "Done title"


def test_a_shrunk_transcript_is_read_from_the_start(transcript) -> None:
    path = transcript("s", user("old prompt"), ai_title("old"))
    read = sessions.read_transcript(path)
    transcript("s", user("new prompt"))  # rewritten, shorter
    again = sessions.read_transcript(path, read)
    assert again is not None
    assert (again.session.first_prompt, again.session.ai_title) == ("new prompt", "")


# --- every directory, and where to start a conversation's Claude -----------------


def test_scan_of_every_directory(claude_home) -> None:
    for cwd, sid in [("/work/a", "sa"), ("/work/b", "sb")]:
        pdir = sessions.project_dir(cwd)
        pdir.mkdir(parents=True)
        (pdir / f"{sid}.jsonl").write_text(
            json.dumps({"sessionId": sid, **user("x", cwd=cwd)}) + "\n"
        )
    assert [s.session_id for s in sessions.scan("/work/a")] == ["sa"]
    assert sorted(s.session_id for s in sessions.scan(None)) == ["sa", "sb"]
    assert sorted(r.head.session_id for r in sessions.build_rows(None)) == ["sa", "sb"]


def test_this_directory_includes_the_folder_of_its_real_path(claude_home, tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    for folder_of, sid in [(str(link), "old"), (str(real), "new")]:
        pdir = sessions.project_dir(folder_of)
        pdir.mkdir(parents=True)
        (pdir / f"{sid}.jsonl").write_text(json.dumps({"sessionId": sid, **user("x")}) + "\n")
    assert sorted(s.session_id for s in sessions.scan(str(link))) == ["new", "old"]


def _session_in(folder_of: str, cwd: str, last_cwd: str = "") -> sessions.Session:
    pdir = sessions.project_dir(folder_of)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / "x.jsonl"
    lines = [user("q", cwd=cwd)] + ([{"type": "attachment", "cwd": last_cwd}] if last_cwd else [])
    path.write_text("".join(json.dumps({"sessionId": "x", **d}) + "\n" for d in lines))
    s = sessions.parse_transcript(path)
    assert s is not None
    return s


def test_launch_dir_is_the_recorded_cwd_when_it_names_the_folder(claude_home, tmp_path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    assert sessions.launch_dir(_session_in(str(work), str(work))) == str(work)


def test_launch_dir_is_found_from_the_folder_name(claude_home, tmp_path) -> None:
    # an older transcript: its folder names one directory, its cwd another
    named = tmp_path / "my.proj" / "app"
    named.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert sessions.launch_dir(_session_in(str(named), str(other))) == str(named)


def test_launch_dir_of_a_worktree_is_the_newest_cwd(claude_home, tmp_path) -> None:
    repo, tree = tmp_path / "repo", tmp_path / "repo" / ".claude" / "worktrees" / "t1"
    tree.mkdir(parents=True)
    s = _session_in(str(tree), str(repo), last_cwd=str(tree))
    assert sessions.launch_dir(s) == str(tree)


def test_launch_dir_of_a_removed_directory_is_none(claude_home, tmp_path) -> None:
    gone = tmp_path / "gone"
    assert sessions.launch_dir(_session_in(str(gone), str(gone))) is None


def test_compact_json_parses_like_spaced_json(transcript) -> None:
    lines = [user("Fix it", branch="dev"), assistant("ok"), ai_title("T"), continued_in("c")]
    spaced = sessions.parse_transcript(transcript("spaced", *lines))
    path = transcript("compact")
    path.write_text(
        "".join(
            json.dumps({"sessionId": "compact", **d}, separators=(",", ":")) + "\n" for d in lines
        )
    )
    compact = sessions.parse_transcript(path)
    assert spaced is not None and compact is not None
    fields = (
        "cwd",
        "last_cwd",
        "git_branch",
        "first_prompt",
        "ai_title",
        "continued_in",
        "has_messages",
    )
    assert [getattr(compact, f) for f in fields] == [getattr(spaced, f) for f in fields]


def test_last_cwd_is_found_deep_in_a_big_line(transcript) -> None:
    big = {
        "type": "user",
        "message": {"role": "user", "content": "x" * 300_000},
        "cwd": "/deep/inside",
        "gitBranch": "main",
    }
    s = sessions.parse_transcript(transcript("big", user("first", cwd="/start"), big))
    assert s is not None and (s.cwd, s.last_cwd) == ("/start", "/deep/inside")


def test_a_grown_transcript_is_not_read_again_from_the_start(transcript) -> None:
    path = transcript("g", user("q"), ai_title("AAA"))
    read = sessions.read_transcript(path)
    # change bytes already read (same length), then append: only the appended line is read
    path.write_text(path.read_text().replace("AAA", "BBB"))
    with path.open("a") as fh:
        fh.write(json.dumps({"sessionId": "g", **assistant("more")}) + "\n")
    again = sessions.read_transcript(path, read)
    assert again is not None and again.session.ai_title == "AAA"


# --- lines as Claude writes them --------------------------------------------------


def test_lines_in_claudes_own_key_order(claude_home) -> None:
    # compact JSON, "type" after parentUuid / isSidechain (user) or after the nested
    # message (assistant), as Claude 2.1.302 writes them; a long cwd
    cwd = "/home/someone/a/fairly/long/path/to/the/project/directory/sessionmux"
    pdir = sessions.project_dir(cwd)
    pdir.mkdir(parents=True)
    path = pdir / "real.jsonl"
    lines = [
        claude_user("real", "Fix the bug", cwd, "feat/x", parent=None),
        claude_assistant("real", "On it.", cwd, "feat/x"),
        claude_user("real", "more", cwd + "/sub", "feat/x"),
        {"type": "ai-title", "aiTitle": "Bug fix", "sessionId": "real"},
        {"type": "custom-title", "customTitle": "mine", "sessionId": "real"},
    ]
    path.write_text("".join(json.dumps(d, separators=(",", ":")) + "\n" for d in lines))
    s = sessions.parse_transcript(path)
    assert s is not None
    assert (s.cwd, s.last_cwd, s.git_branch) == (cwd, cwd + "/sub", "feat/x")
    assert (s.first_prompt, s.ai_title, s.custom_title) == ("Fix the bug", "Bug fix", "mine")
    assert s.has_messages


# --- the scan stops when cancelled -------------------------------------------------


def test_a_cancelled_scan_stops(transcript) -> None:
    import pytest

    for i in range(3):
        transcript(f"s{i}", user("q"))
    checks: list[int] = []

    def cancelled() -> bool:
        checks.append(1)
        return len(checks) > 1  # after the first file

    with pytest.raises(sessions.Cancelled):
        sessions.scan(PROJECT, cancelled)
    assert len(checks) == 2


# --- the sidecar: shape, atomic writes, one writer at a time ------------------------


def test_a_sidecar_of_the_wrong_shape_reads_as_empty_or_partial(claude_home) -> None:
    path = sessions.sidecar_path()
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2]")
    assert sessions.load_meta() == {}
    path.write_text(
        json.dumps(
            {
                "a": "not an object",
                "b": {"label": 3, "group": "g", "tags": ["x", 4]},
                "c": {"tags": "x"},
            }
        )
    )
    assert sessions.load_meta() == {"b": Meta(group="g", tags=("x",)), "c": Meta()}


def test_a_failed_sidecar_write_keeps_the_old_file(claude_home, monkeypatch) -> None:
    import os

    import pytest

    sessions.save_meta({"a": Meta(label="old")})

    def crash(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        sessions.save_meta({"a": Meta(label="new")})
    assert sessions.load_meta() == {"a": Meta(label="old")}


def test_concurrent_meta_updates_all_land(claude_home) -> None:
    import threading
    from dataclasses import replace

    keys = [f"k{i:02}" for i in range(40)]
    start = threading.Barrier(len(keys))

    def edit(key: str) -> None:
        start.wait()
        sessions.update_meta(key, lambda m: replace(m, label=key))

    threads = [threading.Thread(target=edit, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(sessions.load_meta()) == keys  # no read-modify-write lost one


def test_trash_keeps_other_metadata(transcript) -> None:
    transcript("gone", user("q"))
    sessions.save_meta({"gone": Meta(label="x"), "stay": Meta(label="y")})
    (row,) = sessions.build_rows(PROJECT)
    sessions.trash(row)
    assert sessions.load_meta() == {"stay": Meta(label="y")}


def test_tokens_count_each_reply_once_and_the_context_is_the_last_prompt(transcript) -> None:
    path = transcript(
        "s1",
        user("hi"),
        reply("msg_a", (10, 1000, 0), 5),
        reply("msg_a", (10, 1000, 0), 40),  # the same reply, next content block: once
        user("again"),
        reply("msg_b", (3, 50, 1010), 7, model="claude-haiku-4-5"),
        reply("msg_c", (1, 0, 999), 2, sidechain=True),  # a subagent: not this context
        reply("msg_d", (0, 0, 0), 0),  # synthetic: no request was made
        user('a tool result quoting "usage": {"input_tokens": 99999}'),
    )
    s = sessions.parse_transcript(path)
    assert s is not None
    assert s.tokens == Tokens(
        context=1063, input=2073, output=47, model="claude-haiku-4-5", cached=1010
    )


def test_tokens_keep_the_effort_of_the_last_reply(transcript) -> None:
    path = transcript(
        "s1",
        reply("msg_a", (10, 0, 0), 5, effort="high"),
        reply("msg_b", (10, 0, 0), 5, text='"effort": "max" quoted', effort="low"),
    )
    s = sessions.parse_transcript(path)
    assert s is not None
    assert s.tokens.effort == "low"


def test_tokens_of_appended_lines_match_a_full_read(transcript) -> None:
    lines = [user("hi"), reply("msg_a", (10, 1000, 0), 5)]
    path = transcript("s1", *lines)
    first = sessions.read_transcript(path)
    more = [reply("msg_a", (10, 1000, 0), 40), reply("msg_b", (3, 50, 1010), 7)]
    with path.open("a") as fh:
        fh.write("".join(json.dumps({"sessionId": "s1", **d}) + "\n" for d in more))
    grown = sessions.read_transcript(path, first)
    assert grown is not None
    assert grown.session.tokens == Tokens(1063, 2073, 47, "claude-opus-5-5", 1010)


def test_row_tokens_add_the_chain_and_keep_the_newest_context(transcript) -> None:
    transcript("a", user("start"), reply("msg_a", (0, 100, 0), 10), continued_in("b"))
    transcript("b", reply("msg_b", (0, 0, 300), 20, model="claude-sonnet-5-5"))
    rows = sessions.build_rows(PROJECT)
    assert [r.tokens for r in rows] == [Tokens(300, 400, 30, "claude-sonnet-5-5", 300)]


@pytest.mark.parametrize(
    ("model", "window"),
    [
        ("claude-opus-5-5", 1_000_000),
        ("claude-fable-5-1", 1_000_000),
        ("claude-opus-4-6", 1_000_000),
        ("claude-sonnet-4-6", 1_000_000),
        ("claude-sonnet-4-5-20250929", 200_000),
        ("claude-haiku-4-5-20251001", 200_000),
        ("claude-haiku-5-5", 1_000_000),
        ("claude-opus-4-20250514", 200_000),
        ("claude-3-5-sonnet-20241022", 200_000),
        ("claude-sonnet-4-5[1m]", 1_000_000),
        ("<synthetic>", None),
    ],
)
def test_context_window_by_model(model: str, window: int | None) -> None:
    assert sessions.context_window(model) == window


def test_subfolders_adds_the_conversations_inside_but_not_a_lookalike_sibling(
    claude_home,
) -> None:
    for sid, cwd in (("top", PROJECT), ("in", PROJECT + "/sub"), ("out", PROJECT + "-old")):
        pdir = sessions.project_dir(cwd)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{sid}.jsonl").write_text(json.dumps({"sessionId": sid, **user("q", cwd)}) + "\n")
    here = {s.session_id for s in sessions.scan(PROJECT)}
    deep = {s.session_id for s in sessions.scan(PROJECT, subfolders=True)}
    assert here == {"top"} and deep == {"top", "in"}
