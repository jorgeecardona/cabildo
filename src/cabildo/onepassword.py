"""Read secrets with the 1Password CLI (``op``). Stdlib only: cabildo shells out, it has no SDK.

Two calls:

- ``read(ref)`` runs ``op read`` and returns a ``Secret``.
- ``run_argv(env_file, command)`` builds the ``op run --env-file`` command line. ``op run`` puts
  the resolved values in the child's environment, never on its command line, and hides them in
  the child's output.

``op`` is always started from an argument list, never through a shell. A reference must start
with ``op://``, so it cannot be read as an option. An error carries ``op``'s own stderr, which
holds no secret, and never a value.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cabildo.envfile import REF_PREFIX

OP = "op"
_TIMEOUT_S = 15.0
_STDERR_LIMIT = 300

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class OpError(RuntimeError):
    """``op`` is missing, locked, timed out, or could not read the reference."""


@dataclass(frozen=True)
class Secret:
    """A secret value. Its ``repr`` and ``str`` hide it, so a log line or traceback cannot leak it."""

    _value: str

    def reveal(self) -> str:
        """The plain value. Call it only where the value is used."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(<concealed>)"

    __str__ = __repr__


def check_reference(ref: str) -> str:
    """``ref`` when it is an ``op://`` reference.

    Raises:
        OpError: otherwise.
    """
    if not ref.startswith(REF_PREFIX) or len(ref) == len(REF_PREFIX):
        msg = f"not an {REF_PREFIX} reference"
        raise OpError(msg)
    return ref


def read(
    ref: str,
    *,
    runner: Runner = subprocess.run,
    timeout: float = _TIMEOUT_S,
) -> Secret:
    """The secret at ``ref``, from ``op read --no-newline``.

    Raises:
        OpError: when ``ref`` is not a reference, ``op`` is not installed, the call times out,
            ``op`` exits non-zero, or the value is empty.
    """
    argv = [OP, "read", "--no-newline", check_reference(ref)]
    try:
        done = runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        msg = f"the 1Password CLI ({OP}) is not installed"
        raise OpError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"{OP} read timed out after {timeout:g}s (is 1Password locked?)"
        raise OpError(msg) from exc
    if done.returncode != 0:
        detail = (done.stderr or "").strip()[:_STDERR_LIMIT]
        msg = f"{OP} read failed (exit {done.returncode}): {detail}"
        raise OpError(msg)
    if not done.stdout:
        msg = f"{OP} read returned an empty value"
        raise OpError(msg)
    return Secret(done.stdout)


def run_argv(env_file: Path, command: Sequence[str]) -> list[str]:
    """The command line that runs ``command`` with ``env_file`` resolved by ``op run``.

    Raises:
        OpError: when ``command`` is empty.
    """
    if not command:
        msg = "no command to run"
        raise OpError(msg)
    return [OP, "run", "--env-file", str(env_file), "--", *command]
