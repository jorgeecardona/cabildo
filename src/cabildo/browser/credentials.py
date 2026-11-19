"""The login table for ``browser_login``: which 1Password items may be filled into which origin.

The table is ``[browser.login.<site>]`` in the machine-wide ``~/.cabildo/config.toml``. A repo's
committed config is never read for it: a hostile repo could point a site at its own origin.

    [browser.login.github]
    origin   = "https://github.com"
    username = "op://Cabildo/GitHub/username"
    password = "op://Cabildo/GitHub/password"
    otp      = "op://Cabildo/GitHub/one-time password?attribute=otp"   # optional

Without a table, ``discover`` finds the item itself: a 1Password Login whose website URL has the
page's origin. The item's URL stands where ``origin`` stands in the table, so the fill is as
strict. A table entry for the page's origin wins over an item.

This module holds the data and the origin check. It never reads a secret: it only holds the
``op://`` references. An agent names a site, never a reference, so it can only reach what the
human listed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cabildo.onepassword import Item

_HTTPS_PORT = 443
_REF_PREFIX = "op://"
_FORBIDDEN_NETLOC = ("@", "\\")


class CredentialError(ValueError):
    """A login table entry is invalid, or a fill was refused."""


class OriginMismatch(CredentialError):
    """The page is not on the origin the site was configured for."""


@dataclass(frozen=True)
class Origin:
    """A web origin: scheme, lower-case ASCII host and port."""

    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        default = self.scheme == "https" and self.port == _HTTPS_PORT
        return f"{self.scheme}://{self.host}" + ("" if default else f":{self.port}")


@dataclass(frozen=True)
class Credential:
    """One site's login: the origin it is valid on and the references to read."""

    site: str
    origin: Origin
    username: str
    password: str
    otp: str | None = None


@dataclass(frozen=True)
class Table:
    """The valid credentials by site name, and one message per entry that was skipped."""

    by_site: Mapping[str, Credential]
    problems: tuple[str, ...]


def _origin_of(url: str) -> Origin | None:
    """The origin of ``url``, or None when it has no usable host.

    A netloc with ``@`` or a backslash gives None: parsers and browsers disagree on which host
    it means, so the check refuses it.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if any(ch in parts.netloc for ch in _FORBIDDEN_NETLOC) or not parts.hostname:
        return None
    try:
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    scheme = parts.scheme.lower()
    return Origin(scheme, host, port or (_HTTPS_PORT if scheme == "https" else 80))


def parse_origin(value: object) -> Origin:
    """The ``Origin`` for a configured value.

    Raises:
        CredentialError: when the value is not ``https://host[:port]`` alone. A path, query,
            fragment or user info is refused, so the configured origin is exactly what is matched.
    """
    if not isinstance(value, str):
        msg = "origin must be a string"
        raise CredentialError(msg)
    parts = urlsplit(value)
    origin = _origin_of(value)
    if (
        origin is None
        or origin.scheme != "https"
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        msg = f"origin {value!r} must be https://host[:port] with no path, query or user info"
        raise CredentialError(msg)
    return origin


def _ref(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.startswith(_REF_PREFIX):
        msg = f"{key} must be an {_REF_PREFIX} reference"
        raise CredentialError(msg)
    return value


def parse_credential(site: str, entry: Mapping[str, Any]) -> Credential:
    """The ``Credential`` for one ``[browser.login.<site>]`` table.

    Raises:
        CredentialError: on a bad origin or a reference that does not start with ``op://``.
    """
    return Credential(
        site=site,
        origin=parse_origin(entry.get("origin")),
        username=_ref(entry, "username"),
        password=_ref(entry, "password"),
        otp=_ref(entry, "otp") if "otp" in entry else None,
    )


def load(tables: Mapping[str, Any]) -> Table:
    """Parse every ``[browser.login.<site>]`` table. A bad entry is skipped and reported.

    One typo must not turn off the other sites, and must not make a half-valid entry usable.
    """
    by_site: dict[str, Credential] = {}
    problems: list[str] = []
    for site, entry in tables.items():
        if not isinstance(entry, Mapping):
            problems.append(f"{site}: must be a table")
            continue
        try:
            by_site[site] = parse_credential(site, entry)
        except CredentialError as exc:
            problems.append(f"{site}: {exc}")
    return Table(by_site, tuple(problems))


def check_origin(credential: Credential, url: str) -> None:
    """Refuse to fill ``credential`` unless ``url`` is exactly its origin.

    Pass the URL of the frame that holds the field, not only the top page: a page on the right
    origin can embed a frame from another one.

    Raises:
        OriginMismatch: on any difference in scheme, host or port.
    """
    actual = _origin_of(url)
    if actual != credential.origin:
        shown = str(actual) if actual else "an unreadable URL"
        msg = f"{credential.site} is only for {credential.origin}, this page is {shown}"
        raise OriginMismatch(msg)


def at_origin(credentials: Mapping[str, Credential], url: str) -> list[Credential]:
    """The table entries whose origin is exactly the origin of ``url``, by site name."""
    here = _origin_of(url)
    return sorted((c for c in credentials.values() if c.origin == here), key=lambda c: c.site)


def discover(items: Sequence[Item], url: str) -> list[Item]:
    """The Login items with a website URL on exactly the origin of ``url``.

    Only ``https`` counts, and the match is the whole origin: ``github.com.evil.com`` and
    ``http://github.com`` are not ``https://github.com``. A page with no usable origin matches
    nothing.
    """
    here = _origin_of(url)
    if here is None or here.scheme != "https":
        return []
    found = [i for i in items if any(_origin_of(u) == here for u in i.urls)]
    return sorted(found, key=lambda i: (i.title.casefold(), i.vault.casefold()))


def from_item(item: Item, url: str) -> Credential:
    """The ``Credential`` for ``item`` on the origin of ``url``, which ``discover`` matched.

    Raises:
        CredentialError: when ``url`` has no usable origin.
    """
    here = _origin_of(url)
    if here is None:
        msg = "this page has no origin"
        raise CredentialError(msg)
    return Credential(
        site=item.title,
        origin=here,
        username=item.ref("username"),
        password=item.ref("password"),
        otp=item.ref("one-time password?attribute=otp"),
    )
