"""Project detection: a cwd inside a git repo maps to the repo root."""

from __future__ import annotations

import pytest

from cabildo import context as _ctx

# The land daemon's gate imports the main checkout's editable install, which lacks
# this worktree's new darwin ppid/argv backend, so the darwin tests below would error
# there. Skip the module in that case; under `uv run` and GitLab CI (a fresh branch
# install) the backend is present and the whole suite runs.
pytestmark = pytest.mark.skipif(
    not hasattr(_ctx, "_ppid_darwin"),
    reason="darwin backend absent from the installed build; CI verifies it",
)

from cabildo.context import git_project


def test_git_project_finds_repo_root(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)

    root, name = git_project(str(sub))
    assert root == str(repo)
    assert name == "myrepo"


def test_git_project_falls_back_to_cwd_without_repo(tmp_path):
    plain = tmp_path / "loose"
    plain.mkdir()
    root, name = git_project(str(plain))
    assert root == str(plain)
    assert name == "loose"


def _make_worktree(tmp_path, *, commondir=True):
    """Build a repo + one linked worktree. Returns (repo, worktree_dir)."""
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    wt_gitdir = repo / ".git" / "worktrees" / "feature"
    wt_gitdir.mkdir(parents=True)
    if commondir:
        # git writes commondir relative to the worktree gitdir; "../.." -> .git
        (wt_gitdir / "commondir").write_text("../..\n")
    wt = tmp_path / "feature-wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_gitdir}\n")
    return repo, wt


def test_worktree_maps_to_main_repo(tmp_path):
    repo, wt = _make_worktree(tmp_path)
    root, name = git_project(str(wt))
    assert root == str(repo)
    assert name == "myrepo"


def test_cwd_inside_worktree_maps_to_main_repo(tmp_path):
    repo, wt = _make_worktree(tmp_path)
    sub = wt / "src" / "pkg"
    sub.mkdir(parents=True)
    root, name = git_project(str(sub))
    assert root == str(repo)
    assert name == "myrepo"


def test_worktree_without_commondir_still_resolves(tmp_path):
    # Unusual layout: no commondir file -> fall back to gitdir grandparent.
    repo, wt = _make_worktree(tmp_path, commondir=False)
    root, name = git_project(str(wt))
    assert root == str(repo)
    assert name == "myrepo"


# --------------------------------------------------------------------------- #
# Executable identification (the resolve_claude_pid matcher)
# --------------------------------------------------------------------------- #
from cabildo.context import _exe_id


def test_prompt_text_cannot_disqualify_claude():
    # The regression that misattributed messages: a -p prompt mentioning
    # 'cabildo' made the walk skip the agent's own claude process.
    argv = ["claude", "-p", "You are agent BOB on the cabildo bus."]
    exe = _exe_id(argv)
    assert "claude" in exe and "cabildo" not in exe


def test_versioned_claude_binary_matches():
    exe = _exe_id(["/home/u/.local/share/claude/versions/2.1.215", "--print"])
    assert "claude" in exe


def test_node_launched_claude_matches():
    exe = _exe_id(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
    assert "claude" in exe


def test_shell_wrapper_never_matches_even_quoting_claude():
    assert _exe_id(["/bin/bash", "-c", "source ~/.claude/x.sh && claude"]) == ""


def test_cabildo_binaries_are_excluded():
    for argv in (
        ["/home/u/.venv/bin/cabildo-mcp"],
        ["python3", "/home/u/.claude/venv/bin/cabildo-hook", "stop"],
    ):
        exe = _exe_id(argv)
        assert not ("claude" in exe and "cabildo" not in exe)


# --------------------------------------------------------------------------- #
# macOS backend: no /proc there, so _ppid/_argv shell out to `ps` instead.
# These mock `context._ps` (the one subprocess call both darwin helpers go
# through) rather than `subprocess.run` directly, mirroring how test_service.py
# stubs `service.systemctl` instead of the subprocess call underneath it.
# --------------------------------------------------------------------------- #
import subprocess
import sys

from cabildo import context


def _fake_ps(procs: dict[int, tuple[int, str]]):
    """Build a `context._ps` replacement from `{pid: (ppid, command)}`.

    A pid absent from the map behaves exactly like a real `ps -p <pid>` asked
    about a process that no longer exists (or never did): empty stdout,
    regardless of which query (ppid vs command) was being made.
    """

    def fake(*args: str) -> str:
        pid = int(args[-1])
        if pid not in procs:
            return ""
        ppid, command = procs[pid]
        return str(ppid) if "ppid=" in args else command

    return fake


# --- _ps itself: the subprocess boundary ---------------------------------- #
def test_ps_returns_empty_when_binary_missing(monkeypatch):
    def raise_missing(*a, **k):
        raise FileNotFoundError("no such file: ps")

    monkeypatch.setattr(subprocess, "run", raise_missing)
    assert context._ps("-o", "ppid=", "-p", "1") == ""


def test_ps_returns_empty_on_timeout(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=2)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert context._ps("-p", "1") == ""


def test_ps_returns_empty_on_nonzero_exit(monkeypatch):
    """A pid ps can't find (already exited) exits non-zero with empty stdout
    on macOS -- must degrade to "no information", not raise or misparse.
    """

    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    assert context._ps("-p", "999999") == ""


def test_ps_strips_trailing_whitespace(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "  42  \n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    assert context._ps("-p", "1") == "42"


# --- _ppid_darwin / _argv_darwin: parsing ---------------------------------- #
def test_darwin_ppid_parses_ps_output(monkeypatch):
    monkeypatch.setattr(context, "_ps", lambda *a: "4321")
    assert context._ppid_darwin(999) == 4321


def test_darwin_ppid_zero_when_process_gone(monkeypatch):
    monkeypatch.setattr(context, "_ps", lambda *a: "")
    assert context._ppid_darwin(999) == 0


def test_darwin_ppid_zero_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(context, "_ps", lambda *a: "not-a-pid")
    assert context._ppid_darwin(999) == 0


def test_darwin_argv_splits_command_line(monkeypatch):
    monkeypatch.setattr(
        context,
        "_ps",
        lambda *a: "/usr/local/bin/node /opt/homebrew/lib/claude-code/cli.js --resume",
    )
    assert context._argv_darwin(1) == [
        "/usr/local/bin/node",
        "/opt/homebrew/lib/claude-code/cli.js",
        "--resume",
    ]


def test_darwin_argv_empty_when_process_gone(monkeypatch):
    monkeypatch.setattr(context, "_ps", lambda *a: "")
    assert context._argv_darwin(1) == []


def test_darwin_argv_disables_ps_truncation(monkeypatch):
    """Without `-ww`, ps truncates to terminal width and a long
    interpreter+script path (exactly the shape _exe_id needs the tail of)
    would get silently cut off.
    """
    seen: dict[str, tuple[str, ...]] = {}

    def fake_ps(*args: str) -> str:
        seen["args"] = args
        return "node cli.js"

    monkeypatch.setattr(context, "_ps", fake_ps)
    context._argv_darwin(1)
    assert "-ww" in seen["args"]


# --- dispatch: _ppid/_argv route on sys.platform --------------------------- #
def test_ppid_dispatches_to_darwin_backend(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(context, "_ppid_darwin", lambda pid: 777)
    monkeypatch.setattr(
        context, "_ppid_linux", lambda pid: (_ for _ in ()).throw(AssertionError("wrong backend"))
    )
    assert context._ppid(123) == 777


def test_argv_dispatches_to_darwin_backend(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(context, "_argv_darwin", lambda pid: ["x"])
    monkeypatch.setattr(
        context, "_argv_linux", lambda pid: (_ for _ in ()).throw(AssertionError("wrong backend"))
    )
    assert context._argv(123) == ["x"]


def test_ppid_stays_on_linux_backend_off_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(context, "_ppid_linux", lambda pid: 42)
    monkeypatch.setattr(
        context,
        "_ppid_darwin",
        lambda pid: (_ for _ in ()).throw(AssertionError("wrong backend")),
    )
    assert context._ppid(1) == 42


# --- end to end: resolve_claude_pid over the darwin backend ---------------- #
def test_darwin_resolves_claude_through_node_wrapper_ancestry(monkeypatch):
    """Ancestry: pytest (start) -> node-launched claude -> login shell -> init.
    The interpreter wrapper (node cli.js) must be recognized as claude.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    procs = {
        300: (200, "/usr/bin/python3 -m pytest tests/test_x.py"),
        200: (
            100,
            "/usr/local/bin/node /opt/homebrew/lib/claude-code/cli.js -p hello",
        ),
        100: (1, "/bin/zsh -c 'claude'"),
    }
    monkeypatch.setattr(context, "_ps", _fake_ps(procs))
    assert context.resolve_claude_pid(300) == 200


def test_darwin_prompt_text_does_not_disqualify_ancestor_claude(monkeypatch):
    """Ambiguous args: claude's own argv can quote 'cabildo' (e.g. a -p prompt
    telling it to talk on the bus) -- that must not make the walk skip it.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    procs = {
        20: (10, "/usr/bin/python3 tool.py"),
        10: (1, "/usr/local/bin/claude -p talk on the cabildo bus as agent bob"),
    }
    monkeypatch.setattr(context, "_ps", _fake_ps(procs))
    assert context.resolve_claude_pid(20) == 10


def test_darwin_shell_wrapper_quoting_claude_is_not_claude(monkeypatch):
    """Ambiguous args, the other direction: a bash -c string that *mentions*
    claude is a shell, not claude -- the walk must not stop there.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    procs = {
        20: (10, "/usr/bin/python3 tool.py"),
        10: (1, "/bin/bash -c source ~/.claude/x.sh && claude"),
        # walk continues past 10 (a shell, not claude) and finds nothing above it
    }
    monkeypatch.setattr(context, "_ps", _fake_ps(procs))
    assert context.resolve_claude_pid(20) == 0


def test_darwin_disappearing_process_stops_walk_conservatively(monkeypatch):
    """Process exit mid-walk: pid 100 has already exited by the time we ask
    `ps` about it (a real race, or the tail end of PID reuse). `ps` reports
    nothing for it -- the walk must end with "no claude found", never guess.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    procs = {50: (100, "/usr/bin/python3 something.py")}  # 100 itself: not in the map
    monkeypatch.setattr(context, "_ps", _fake_ps(procs))
    assert context.resolve_claude_pid(50) == 0


def test_darwin_pid_reused_by_unrelated_process_is_not_misattributed(monkeypatch):
    """PID reuse: pid 100 used to be claude but exited; the OS handed pid 100
    to an unrelated process (Slack). Nothing here caches identity across
    calls, so asking about pid 100 now must see Slack -- and correctly
    conclude the chain holds no claude, never the stale identity.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    procs = {
        50: (100, "/usr/bin/python3 something.py"),
        100: (1, "/Applications/Slack.app/Contents/MacOS/Slack"),
    }
    monkeypatch.setattr(context, "_ps", _fake_ps(procs))
    assert context.resolve_claude_pid(50) == 0


def test_darwin_cabildo_binary_excluded_from_ancestry(monkeypatch):
    """A cabildo hook/MCP process installed under a claude-ish path must never
    match itself while walking up from a tool call.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    procs = {
        5: (1, "/home/u/.venv/bin/cabildo-mcp"),
    }
    monkeypatch.setattr(context, "_ps", _fake_ps(procs))
    assert context.resolve_claude_pid(5) == 0
