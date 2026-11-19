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
