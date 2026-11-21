"""Read secrets with the 1Password CLI (``op``). Stdlib only: cabildo shells out, it has no SDK.

Three calls:

- ``read(ref)`` runs ``op read`` and returns a ``Secret``.
- ``list_logins()`` runs ``op item list`` and returns the Login items with their website URLs.
  It reads no secret: titles, ids and URLs only.
- ``run_argv(env_file, command)`` builds the ``op run --env-file`` command line. ``op run`` puts
  the resolved values in the child's environment, never on its command line, and hides them in
  the child's output.

``op`` is always started from an argument list, never through a shell. A reference must start
with ``op://``, so it cannot be read as an option. An error carries ``op``'s own stderr, which
holds no secret, and never a value.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cabildo import envfile
from cabildo.envfile import REF_PREFIX

OP = "op"
_TIMEOUT_S = 60.0  # a human may need to unlock 1Password or approve a prompt
_STDERR_LIMIT = 300
_ID = re.compile(r"[a-z0-9]{26}")  # the id 1Password gives a vault or an item
_SIGNED_OUT = ("not signed in", "authorization prompt dismissed", "no accounts configured")
_UNLOCK_HINT = (
    "unlock 1Password and turn on Settings > Developer > Integrate with 1Password CLI, "
    "then try again"
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"  # noqa: S105


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


@dataclass(frozen=True)
class Item:
    """A Login item as ``op item list`` shows it: no secret, only what finds it."""

    id: str
    title: str
    vault_id: str
    vault: str
    urls: tuple[str, ...]

    def ref(self, field: str) -> str:
        """The ``op://`` reference of one field. Ids, not names, so no name can alter the path."""
        return f"{REF_PREFIX}{self.vault_id}/{self.id}/{field}"


def env_path() -> Path:
    """``cabildo/env`` in the user's config folder: the file that holds the service account token."""
    root = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(root).expanduser() / "cabildo" / "env"


def _auth() -> dict[str, dict[str, str]]:
    """The ``env`` for an ``op`` call: the service account token of ``~/.config/cabildo/env``.

    With ``OP_SERVICE_ACCOUNT_TOKEN`` in that file, ``op`` reaches only the vaults of that service
    account, not your whole account. The variable of the process wins when it is set. A token
    line that cannot be used is an error: a call must never fall back to the account of the
    desktop app.

    Raises:
        OpError: when the file has the token line but it is empty, an ``op://`` reference, in a
            file that others can read, or the file cannot be parsed.
    """
    path = env_path()
    if TOKEN_ENV in os.environ or not path.exists():
        return {}
    try:
        entries = envfile.parse(path.read_text(encoding="utf-8")).entries
        mode = stat.S_IMODE(path.stat().st_mode)
    except (OSError, envfile.EnvFileError) as exc:
        msg = f"cannot read {path}: {exc}"
        raise OpError(msg) from exc
    token = next((e for e in entries if e.name == TOKEN_ENV), None)
    if token is None:
        return {}
    problem = (
        "is empty"
        if not token.value
        else "must be the token, not an op:// reference"
        if token.reference
        else "is in a file that others can read: run chmod 600"
        if mode & 0o077
        else ""
    )
    if problem:
        msg = f"{TOKEN_ENV} in {path} {problem}: not falling back to your full 1Password account"
        raise OpError(msg)
    return {"env": {**os.environ, TOKEN_ENV: token.value}}


def _failure(what: str, code: int, stderr: str) -> OpError:
    """An ``OpError`` with ``op``'s stderr, and the fix when 1Password is locked or signed out."""
    detail = (stderr or "").strip()[:_STDERR_LIMIT]
    hint = f". To fix: {_UNLOCK_HINT}" if any(m in detail.lower() for m in _SIGNED_OUT) else ""
    return OpError(f"{OP} {what} failed (exit {code}): {detail}{hint}")


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
    auth = _auth()
    try:
        done = runner(argv, capture_output=True, text=True, timeout=timeout, check=False, **auth)
    except FileNotFoundError as exc:
        msg = f"the 1Password CLI ({OP}) is not installed"
        raise OpError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"{OP} read timed out after {timeout:g}s (is 1Password locked?)"
        raise OpError(msg) from exc
    if done.returncode != 0:
        raise _failure("read", done.returncode, done.stderr)
    if not done.stdout:
        msg = f"{OP} read returned an empty value"
        raise OpError(msg)
    return Secret(done.stdout)


def _item_of(raw: object) -> Item | None:
    """The ``Item`` for one entry of ``op``'s JSON, or None when it lacks an id or a vault."""
    if not isinstance(raw, dict):
        return None
    vault = raw.get("vault")
    vault = vault if isinstance(vault, dict) else {}
    item_id, vault_id = raw.get("id"), vault.get("id")
    if not (isinstance(item_id, str) and _ID.fullmatch(item_id)):
        return None
    if not (isinstance(vault_id, str) and _ID.fullmatch(vault_id)):
        return None
    urls = raw.get("urls")
    hrefs = tuple(
        u["href"]
        for u in (urls if isinstance(urls, list) else ())
        if isinstance(u, dict) and isinstance(u.get("href"), str)
    )
    return Item(
        id=item_id,
        title=str(raw.get("title") or item_id),
        vault_id=vault_id,
        vault=str(vault.get("name") or vault_id),
        urls=hrefs,
    )


def _run_json(
    argv: list[str], what: str, *, runner: Runner, timeout: float, stdin: str | None = None
) -> list[object]:
    auth = _auth()
    try:
        done = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            input=stdin,
            **auth,
        )
    except FileNotFoundError as exc:
        msg = f"the 1Password CLI ({OP}) is not installed"
        raise OpError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"{OP} {what} timed out after {timeout:g}s (is 1Password locked?)"
        raise OpError(msg) from exc
    if done.returncode != 0:
        raise _failure(what, done.returncode, done.stderr)
    try:
        data = json.loads(done.stdout)
    except ValueError as exc:
        msg = f"{OP} {what} did not return JSON"
        raise OpError(msg) from exc
    return data if isinstance(data, list) else [data]


def list_logins(*, runner: Runner = subprocess.run, timeout: float = _TIMEOUT_S) -> list[Item]:
    """The Login items of every vault the CLI can read, with their website URLs.

    ``op item list`` should carry the URLs. When it carries none for any item, the items go back
    through ``op item get -``, which always does.

    Raises:
        OpError: when ``op`` is missing, locked, timed out, or exits non-zero.
    """
    listing = _run_json(
        [OP, "item", "list", "--categories", "Login", "--format", "json"],
        "item list",
        runner=runner,
        timeout=timeout,
    )
    rows = [r for r in listing if isinstance(r, dict)]
    if rows and not any(r.get("urls") for r in rows):
        rows = [
            r
            for r in _run_json(
                [OP, "item", "get", "-", "--format", "json"],
                "item get",
                runner=runner,
                timeout=timeout,
                stdin=json.dumps(rows),
            )
            if isinstance(r, dict)
        ]
    return [item for r in rows if (item := _item_of(r))]


def run_argv(env_file: Path, command: Sequence[str]) -> list[str]:
    """The command line that runs ``command`` with ``env_file`` resolved by ``op run``.

    Raises:
        OpError: when ``command`` is empty.
    """
    if not command:
        msg = "no command to run"
        raise OpError(msg)
    return [OP, "run", "--env-file", str(env_file), "--", *command]
