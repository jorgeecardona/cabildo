"""envfile.py: the .env reader keeps op:// references as text and never prints a value."""

from __future__ import annotations

import pytest

from cabildo import envfile


def names(text: str) -> dict[str, str]:
    return {e.name: e.value for e in envfile.parse(text).entries}


def test_reads_plain_export_quoted_and_commented_lines():
    text = (
        "# a comment\n"
        "\n"
        "A=1\n"
        "export B=two words\n"
        'C="say \\"hi\\"\\nnow"\n'
        "D='lit\\n$X'\n"
        "E=value   # trailing\n"
        'F="q" # after quote\n'
        "G=a#b\n"
        "H=\n"
    )
    assert names(text) == {
        "A": "1",
        "B": "two words",
        "C": 'say "hi"\nnow',
        "D": "lit\\n$X",
        "E": "value",
        "F": "q",
        "G": "a#b",
        "H": "",
    }


def test_references_are_flagged_and_stay_text():
    f = envfile.parse("TOKEN=op://Cabildo/wan/token\nPLAIN=abc\nQ='op://V/I/f'\n")
    assert [e.reference for e in f.entries] == [True, False, True]
    assert [e.name for e in f.plain] == ["PLAIN"]


def test_a_reference_with_dollar_is_refused():
    with pytest.raises(envfile.EnvFileError) as err:
        envfile.parse("A=1\nT=op://$VAULT/item/field\n")
    assert err.value.line == 2


def test_dollar_in_a_plain_value_is_kept_not_expanded():
    assert names("A=$HOME/x\n") == {"A": "$HOME/x"}


@pytest.mark.parametrize(
    "text",
    ["1BAD=x", "NOEQUALS", "A B=c", "=x", "A=1\nA=2", 'A="open', "A='open", 'A="x" y'],
)
def test_bad_lines_raise_with_a_line_number(text):
    with pytest.raises(envfile.EnvFileError):
        envfile.parse(text)


def test_errors_and_repr_never_hold_a_plain_value():
    with pytest.raises(envfile.EnvFileError) as err:
        envfile.parse('A="hunter2 open')
    assert "hunter2" not in str(err.value)
    entry = envfile.parse("A=hunter2\n").entries[0]
    assert "hunter2" not in repr(entry)
    assert "hunter2" not in repr(envfile.parse("A=hunter2\n"))


def test_read_reads_a_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=op://V/I/f\n")
    assert envfile.read(p).entries[0].reference
