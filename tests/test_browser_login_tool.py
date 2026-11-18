"""browser_login: fills from 1Password only on the right origin, and hides the secret after."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import pytest
from mcp.server.fastmcp import FastMCP

from cabildo import onepassword
from cabildo.browser import credentials, server
from cabildo.browser.server import Session

T = TypeVar("T")
GITHUB = "https://github.com"
ENTRY = {
    "origin": GITHUB,
    "username": "op://V/GitHub/username",
    "password": "op://V/GitHub/password",
    "otp": "op://V/GitHub/otp?attribute=otp",
}
VALUES = {
    ENTRY["username"]: "octocat",
    ENTRY["password"]: "hunter2-secret",
    ENTRY["otp"]: "493021",
}


class Field:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page, self.selector = page, selector

    async def evaluate(self, _js: str) -> list[str]:
        origin, tag, kind = self.page.fields[self.selector]
        return [origin, tag, kind]

    async def fill(self, text: str) -> None:
        self.page.log.append(("fill", self.selector, text))

    async def press(self, key: str) -> None:
        self.page.log.append(("press", self.selector, key))


class FakePage:
    frames: ClassVar[list[Any]] = []

    def __init__(self) -> None:
        self.url = f"{GITHUB}/login"
        self.log: list[tuple[str, ...]] = []
        self.fields = {
            "aria-ref=e1": (GITHUB, "INPUT", "text"),
            "aria-ref=e2": (GITHUB, "INPUT", "password"),
            "aria-ref=e3": (GITHUB, "INPUT", "text"),
        }
        self.echo = ""

    def locator(self, selector: str) -> Field:
        return Field(self, selector)

    async def title(self) -> str:
        return "Sign in"

    async def aria_snapshot(self, **_kw: Any) -> str:
        return f'- textbox "Password": {self.echo}'

    async def evaluate(self, _js: str) -> str:
        return "ok"


class FakeSession(Session):
    def __post_init__(self) -> None:
        self.fake = FakePage()

    async def page(self) -> Any:
        return self.fake

    async def close(self) -> None:
        self.concealed.clear()


def make(tmp_path: Path, reads: list[str] | None = None) -> tuple[FakeSession, FastMCP]:
    def read(ref: str) -> onepassword.Secret:
        if reads is not None:
            reads.append(ref)
        return onepassword.Secret(VALUES[ref])

    session = FakeSession(
        output_dir=tmp_path,
        logins=credentials.load({"github": ENTRY}).by_site,
        read_secret=read,
    )
    session.__post_init__()
    return session, server.build(session)


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


def call(mcp: FastMCP, name: str, args: dict[str, Any] | None = None) -> str:
    out = run(mcp.call_tool(name, args or {}))
    blocks = out[0] if isinstance(out, tuple) else out
    return blocks[0].text  # type: ignore[union-attr]


LOGIN = {"site": "github", "username_target": "e1", "password_target": "e2"}


def test_fills_username_and_password_and_never_returns_them(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    session.fake.echo = VALUES[ENTRY["password"]]
    text = call(mcp, "browser_login", LOGIN)
    assert ("fill", "aria-ref=e1", "octocat") in session.fake.log
    assert ("fill", "aria-ref=e2", "hunter2-secret") in session.fake.log
    assert "hunter2-secret" not in text
    assert "<concealed>" in text
    assert text.startswith("filled github")


def test_submit_presses_enter_in_the_last_field(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    call(mcp, "browser_login", {**LOGIN, "otp_target": "e3", "submit": True})
    assert ("fill", "aria-ref=e3", "493021") in session.fake.log
    assert session.fake.log[-1] == ("press", "aria-ref=e3", "Enter")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/login",
        "https://github.com.evil.com/",
        "http://github.com/login",
        "about:blank",
    ],
)
def test_a_page_on_another_origin_reads_and_fills_nothing(tmp_path: Path, url: str) -> None:
    reads: list[str] = []
    session, mcp = make(tmp_path, reads)
    session.fake.url = url
    text = call(mcp, "browser_login", LOGIN)
    assert text.startswith("refused:")
    assert reads == []
    assert session.fake.log == []
    assert session.concealed == set()


def test_a_field_in_a_frame_on_another_origin_reads_and_fills_nothing(tmp_path: Path) -> None:
    reads: list[str] = []
    session, mcp = make(tmp_path, reads)
    session.fake.fields["aria-ref=e2"] = ("https://evil.com", "INPUT", "password")
    text = call(mcp, "browser_login", LOGIN)
    assert text.startswith("refused:")
    assert reads == []
    assert session.fake.log == []


@pytest.mark.parametrize(
    "info",
    [(GITHUB, "INPUT", "text"), (GITHUB, "DIV", ""), (GITHUB, "INPUT", "checkbox")],
)
def test_the_password_goes_only_into_a_password_field(
    tmp_path: Path, info: tuple[str, ...]
) -> None:
    session, mcp = make(tmp_path)
    session.fake.fields["aria-ref=e2"] = info  # type: ignore[assignment]
    text = call(mcp, "browser_login", LOGIN)
    assert text.startswith("refused:") or info[2] == "checkbox"
    assert ("fill", "aria-ref=e2", "hunter2-secret") not in session.fake.log


def test_the_username_never_goes_into_a_password_field(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    text = call(mcp, "browser_login", {**LOGIN, "username_target": "e2"})
    assert text.startswith("refused:")
    assert session.fake.log == []


def test_an_unknown_site_names_the_configured_ones_only(tmp_path: Path) -> None:
    _session, mcp = make(tmp_path)
    text = call(mcp, "browser_login", {**LOGIN, "site": "op://V/x/y"})
    assert text.startswith("refused:")
    assert "github" in text
    assert "op://V" not in text.split("Configured")[1]


def test_otp_without_a_reference_is_refused(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    entry = {k: v for k, v in ENTRY.items() if k != "otp"}
    session.logins = credentials.load({"github": entry}).by_site
    text = call(mcp, "browser_login", {**LOGIN, "otp_target": "e3"})
    assert text.startswith("refused:")


def test_an_op_failure_is_reported_without_a_value(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)

    def locked(_ref: str) -> onepassword.Secret:
        msg = "op read timed out after 15s (is 1Password locked?)"
        raise onepassword.OpError(msg)

    session.read_secret = locked
    text = call(mcp, "browser_login", LOGIN)
    assert text.startswith("refused: could not read from 1Password")
    assert session.fake.log == []


def test_evaluate_is_refused_while_a_secret_is_in_the_page(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    call(mcp, "browser_login", LOGIN)
    assert call(mcp, "browser_evaluate", {"function": "() => 1"}).startswith("refused")
    session.concealed.clear()  # what a main-frame navigation does
    assert call(mcp, "browser_evaluate", {"function": "() => 1"}) == '"ok"'


def test_console_and_network_output_are_redacted(tmp_path: Path) -> None:
    session, mcp = make(tmp_path)
    call(mcp, "browser_login", LOGIN)
    session.console.append(("error", "bad password hunter2-secret"))
    session.requests.append("[GET] https://github.com/x?p=hunter2-secret => 200")
    assert "hunter2-secret" not in call(mcp, "browser_console_messages")
    assert "hunter2-secret" not in call(mcp, "browser_network_requests")


def test_a_main_frame_navigation_forgets_the_secrets_and_a_subframe_does_not() -> None:
    class Frame:
        def __init__(self, parent: object | None) -> None:
            self.parent_frame = parent

    session = Session()
    session.concealed.add("s3cret")
    top = Frame(None)
    session._on_navigated(Frame(top))
    assert session.concealed == {"s3cret"}
    session._on_navigated(top)
    assert session.concealed == set()


def test_redact_replaces_the_longest_secret_first() -> None:
    session = Session()
    session.concealed.update({"abc", "abcdef"})
    assert session.redact("x abcdef y abc") == "x <concealed> y <concealed>"


def test_the_server_offers_browser_login(tmp_path: Path) -> None:
    _session, mcp = make(tmp_path)
    assert "browser_login" in {t.name for t in run(mcp.list_tools())}


def test_a_second_page_call_fills_only_the_code(tmp_path: Path) -> None:
    reads: list[str] = []
    session, mcp = make(tmp_path, reads)
    text = call(mcp, "browser_login", {"site": "github", "otp_target": "e3", "submit": True})
    assert text.startswith("filled github")
    assert reads == [ENTRY["otp"]]
    assert session.fake.log == [
        ("fill", "aria-ref=e3", "493021"),
        ("press", "aria-ref=e3", "Enter"),
    ]
    assert "493021" not in text


def test_a_password_only_page_fills_only_the_password(tmp_path: Path) -> None:
    reads: list[str] = []
    session, mcp = make(tmp_path, reads)
    call(mcp, "browser_login", {"site": "github", "password_target": "e2"})
    assert reads == [ENTRY["password"]]
    assert session.fake.log == [("fill", "aria-ref=e2", "hunter2-secret")]


def test_no_target_at_all_reads_nothing(tmp_path: Path) -> None:
    reads: list[str] = []
    session, mcp = make(tmp_path, reads)
    text = call(mcp, "browser_login", {"site": "github"})
    assert text.startswith("refused: give at least one")
    assert reads == []
    assert session.fake.log == []


def test_the_code_alone_is_still_origin_checked(tmp_path: Path) -> None:
    reads: list[str] = []
    session, mcp = make(tmp_path, reads)
    session.fake.url = "https://evil.com/2fa"
    text = call(mcp, "browser_login", {"site": "github", "otp_target": "e3"})
    assert text.startswith("refused:")
    assert reads == []
