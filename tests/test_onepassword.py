"""onepassword.py: op read and op run command lines, with a fake runner (op is not required)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cabildo import onepassword as op


class Fake:
    def __init__(self, *, rc=0, out="s3cret", err="", raises=None):
        self.rc, self.out, self.err, self.raises = rc, out, err, raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.err)


REF = "op://Cabildo/GitHub/password"


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty config folder, so a test never reads the real ``~/.config/cabildo/env``."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv(op.TOKEN_ENV, raising=False)
    return tmp_path / "cfg" / "cabildo"


def write_env(folder: Path, text: str, mode: int = 0o600) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "env"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_read_returns_a_concealed_secret_from_an_argument_list():
    fake = Fake()
    secret = op.read(REF, runner=fake)
    assert secret.reveal() == "s3cret"
    argv, kw = fake.calls[0]
    assert argv == ["op", "read", "--no-newline", REF]
    assert kw["capture_output"] is True
    assert kw["timeout"] > 0
    assert "shell" not in kw


def test_a_secret_never_prints_its_value():
    secret = op.read(REF, runner=Fake())
    assert "s3cret" not in repr(secret)
    assert "s3cret" not in str(secret)
    assert "s3cret" not in f"{secret}"
    assert "s3cret" not in repr([secret])


@pytest.mark.parametrize("ref", ["--help", "-n", "password", "", "op://", "https://x"])
def test_read_refuses_anything_but_a_reference_before_running_op(ref):
    fake = Fake()
    with pytest.raises(op.OpError):
        op.read(ref, runner=fake)
    assert fake.calls == []


def test_read_reports_op_stderr_not_the_value():
    fake = Fake(rc=1, out="s3cret", err="[ERROR] 'GitHub' isn't an item in the vault")
    with pytest.raises(op.OpError) as err:
        op.read(REF, runner=fake)
    assert "isn't an item" in str(err.value)
    assert "s3cret" not in str(err.value)


def test_read_explains_a_missing_op():
    with pytest.raises(op.OpError, match="not installed"):
        op.read(REF, runner=Fake(raises=FileNotFoundError()))


def test_read_explains_a_timeout():
    boom = subprocess.TimeoutExpired(["op"], 15)
    with pytest.raises(op.OpError, match="timed out"):
        op.read(REF, runner=Fake(raises=boom))


def test_read_refuses_an_empty_value():
    with pytest.raises(op.OpError, match="empty"):
        op.read(REF, runner=Fake(out=""))


def test_run_argv_puts_the_command_after_a_double_dash():
    assert op.run_argv(Path("/p/.env"), ["make", "up", "--flag"]) == [
        "op",
        "run",
        "--env-file",
        "/p/.env",
        "--",
        "make",
        "up",
        "--flag",
    ]


def test_run_argv_refuses_an_empty_command():
    with pytest.raises(op.OpError):
        op.run_argv(Path(".env"), [])


ID_V, ID_A, ID_B = "v" * 26, "a" * 26, "b" * 26
LISTING = (
    '[{"id": "%s", "title": "GitHub", "vault": {"id": "%s", "name": "Personal"},'
    ' "urls": [{"primary": true, "href": "https://github.com/login"}]},'
    ' {"id": "not-an-id", "title": "Bad", "vault": {"id": "%s"}, "urls": []},'
    ' {"id": "%s", "title": "No urls", "vault": {"id": "%s"}}]'
) % (ID_A, ID_V, ID_V, ID_B, ID_V)


def test_list_logins_returns_items_with_urls_and_skips_bad_ids():
    fake = Fake(out=LISTING)
    items = op.list_logins(runner=fake)
    assert [i.title for i in items] == ["GitHub", "No urls"]
    assert items[0].urls == ("https://github.com/login",)
    assert items[0].ref("password") == f"op://{ID_V}/{ID_A}/password"
    argv, kw = fake.calls[0]
    assert argv == ["op", "item", "list", "--categories", "Login", "--format", "json"]
    assert "shell" not in kw


def test_list_logins_falls_back_to_item_get_when_the_list_has_no_urls():
    bare = f'[{{"id": "{ID_A}", "title": "GitHub", "vault": {{"id": "{ID_V}"}}}}]'
    full = (
        f'[{{"id": "{ID_A}", "title": "GitHub", "vault": {{"id": "{ID_V}", "name": "P"}},'
        ' "urls": [{"href": "https://github.com"}]}]'
    )
    outs = iter([bare, full])
    calls: list[tuple[list[str], dict]] = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, next(outs), "")

    items = op.list_logins(runner=runner)
    assert items[0].urls == ("https://github.com",)
    assert calls[1][0] == ["op", "item", "get", "-", "--format", "json"]
    assert ID_A in calls[1][1]["input"]


def test_a_signed_out_op_says_how_to_fix_it():
    fake = Fake(rc=1, err="[ERROR] 2026/11/19 15:05:10 account is not signed in")
    with pytest.raises(op.OpError) as err:
        op.list_logins(runner=fake)
    assert "Integrate with 1Password CLI" in str(err.value)
    with pytest.raises(op.OpError) as err2:
        op.read(REF, runner=Fake(rc=1, err="authorization prompt dismissed, please try again"))
    assert "Integrate with 1Password CLI" in str(err2.value)


def test_list_logins_reports_bad_json_and_a_missing_op():
    with pytest.raises(op.OpError, match="did not return JSON"):
        op.list_logins(runner=Fake(out="nope"))
    with pytest.raises(op.OpError, match="not installed"):
        op.list_logins(runner=Fake(raises=FileNotFoundError()))


def test_no_env_file_means_no_token_and_op_gets_no_env_argument():
    fake = Fake()
    op.read(REF, runner=fake)
    assert "env" not in fake.calls[0][1]


def test_the_token_in_the_env_file_reaches_op_and_other_lines_are_ignored(config_home):
    write_env(config_home, "CABILDO_TOKEN=abc\nOP_SERVICE_ACCOUNT_TOKEN=ops_secret\n")
    fake = Fake()
    op.read(REF, runner=fake)
    assert fake.calls[0][1]["env"][op.TOKEN_ENV] == "ops_secret"
    op.list_logins(runner=Fake(out="[]"))


def test_a_file_without_the_token_line_leaves_op_on_its_own_login(config_home):
    write_env(config_home, "CABILDO_TOKEN=abc\n")
    fake = Fake()
    op.read(REF, runner=fake)
    assert "env" not in fake.calls[0][1]


def test_the_process_variable_wins_and_the_file_is_not_read(config_home, monkeypatch):
    write_env(config_home, "OP_SERVICE_ACCOUNT_TOKEN=\n")
    monkeypatch.setenv(op.TOKEN_ENV, "ops_from_process")
    fake = Fake()
    op.read(REF, runner=fake)
    assert "env" not in fake.calls[0][1]  # op inherits the variable as it is


@pytest.mark.parametrize(
    ("text", "mode", "reason"),
    [
        ("OP_SERVICE_ACCOUNT_TOKEN=\n", 0o600, "is empty"),
        ("OP_SERVICE_ACCOUNT_TOKEN=op://V/I/f\n", 0o600, "not an op:// reference"),
        ("OP_SERVICE_ACCOUNT_TOKEN=ops_x\n", 0o644, "chmod 600"),
    ],
)
def test_an_unusable_token_line_stops_the_call_and_never_falls_back(
    config_home, text, mode, reason
):
    write_env(config_home, text, mode)
    fake = Fake()
    with pytest.raises(op.OpError, match=reason) as err:
        op.read(REF, runner=fake)
    assert fake.calls == []
    assert "ops_x" not in str(err.value)


def test_an_env_file_that_cannot_be_parsed_stops_the_call(config_home):
    write_env(config_home, "not a line\n")
    fake = Fake()
    with pytest.raises(op.OpError, match="cannot read"):
        op.list_logins(runner=fake)
    assert fake.calls == []
