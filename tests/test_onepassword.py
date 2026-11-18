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
