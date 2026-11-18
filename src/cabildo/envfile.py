"""Read a ``.env`` file into names and values, keeping ``op://`` references unresolved.

The reader never reads a secret from 1Password and never expands ``$VAR``. A value that starts
with ``op://`` is a reference: it stays text until ``onepassword.run_argv`` hands the file to
``op run``. An error names the line, never the value.

Accepted lines:

    # comment
    NAME=value
    export NAME=value
    NAME="double quoted"     (escapes: backslash-n, backslash-quote, double backslash)
    NAME='single quoted, literal'
    NAME=value   # a comment after a space
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REF_PREFIX = "op://"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPES = {"n": "\n", '"': '"', "\\": "\\"}


class EnvFileError(ValueError):
    """A line of the file cannot be read. The message never holds a value."""

    def __init__(self, line: int, reason: str) -> None:
        super().__init__(f"line {line}: {reason}")
        self.line = line


@dataclass(frozen=True)
class Entry:
    """One variable. ``reference`` is true when the value is an ``op://`` reference."""

    name: str
    value: str
    line: int

    @property
    def reference(self) -> bool:
        """Whether ``value`` is a 1Password reference."""
        return self.value.startswith(REF_PREFIX)

    def __repr__(self) -> str:
        shown = self.value if self.reference else "<plain>"
        return f"Entry({self.name}={shown})"


@dataclass(frozen=True)
class EnvFile:
    """The entries of a ``.env`` file, in file order."""

    entries: tuple[Entry, ...]

    @property
    def plain(self) -> tuple[Entry, ...]:
        """The entries that hold a secret-looking value in the file itself, not a reference."""
        return tuple(e for e in self.entries if not e.reference)


def _quoted(rest: str, quote: str, line: int) -> tuple[str, str]:
    """The value inside ``rest`` (which starts after the opening quote) and what follows it."""
    out: list[str] = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == quote:
            return "".join(out), rest[i + 1 :]
        if quote == '"' and ch == "\\" and i + 1 < len(rest) and rest[i + 1] in _ESCAPES:
            out.append(_ESCAPES[rest[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    raise EnvFileError(line, "unterminated quote (multi-line values are not supported)")


def _value(raw: str, line: int) -> str:
    raw = raw.lstrip()
    if raw[:1] in {'"', "'"}:
        value, tail = _quoted(raw[1:], raw[0], line)
        tail = tail.strip()
        if tail and not tail.startswith("#"):
            raise EnvFileError(line, "text after the closing quote")
        return value
    cut = re.search(r"\s#", raw)
    return (raw[: cut.start()] if cut else raw).strip()


def _check_reference(value: str, line: int) -> None:
    # op run expands $VAR inside a reference; refusing it keeps the vault item a fixed string.
    if value.startswith(REF_PREFIX) and "$" in value:
        raise EnvFileError(line, "an op:// reference must not contain '$'")


def parse(text: str) -> EnvFile:
    """The ``EnvFile`` for ``text``.

    Raises:
        EnvFileError: on a bad name, a duplicate name, an unterminated quote, text after a
            closing quote, or a ``$`` inside an ``op://`` reference.
    """
    entries: list[Entry] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        name, sep, rest = line.partition("=")
        name = name.strip()
        if not sep or not _NAME.fullmatch(name):
            raise EnvFileError(number, "expected NAME=value")
        if name in seen:
            raise EnvFileError(number, f"{name} is set twice")
        value = _value(rest, number)
        _check_reference(value, number)
        seen.add(name)
        entries.append(Entry(name, value, number))
    return EnvFile(tuple(entries))


def read(path: Path) -> EnvFile:
    """Parse the file at ``path``.

    Raises:
        EnvFileError: as ``parse``.
        OSError: when the file cannot be read.
    """
    return parse(path.read_text(encoding="utf-8"))
