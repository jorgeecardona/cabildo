"""credentials.py: the login table and the origin check that guards every fill."""

from __future__ import annotations

import pytest

from cabildo import config
from cabildo.browser import credentials as cr

ENTRY = {
    "origin": "https://github.com",
    "username": "op://Cabildo/GitHub/username",
    "password": "op://Cabildo/GitHub/password",
}


def cred(**over: object) -> cr.Credential:
    return cr.parse_credential("github", {**ENTRY, **over})


def test_parse_reads_origin_refs_and_optional_otp():
    c = cred(otp="op://Cabildo/GitHub/one-time password?attribute=otp")
    assert c.origin == cr.Origin("https", "github.com", 443)
    assert c.password == ENTRY["password"]
    assert c.otp is not None
    assert cred().otp is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://github.com",
        "https://github.com/login",
        "https://github.com?x=1",
        "https://user@github.com",
        "https://github.com\\@evil.com",
        "github.com",
        "",
        5,
        None,
    ],
)
def test_parse_refuses_anything_but_a_bare_https_origin(origin):
    with pytest.raises(cr.CredentialError):
        cred(origin=origin)


@pytest.mark.parametrize("key", ["username", "password", "otp"])
def test_parse_refuses_a_value_that_is_not_an_op_reference(key):
    with pytest.raises(cr.CredentialError):
        cred(**{key: "hunter2"})


def test_missing_password_is_refused():
    entry = {k: v for k, v in ENTRY.items() if k != "password"}
    with pytest.raises(cr.CredentialError):
        cr.parse_credential("github", entry)


def test_load_skips_a_bad_site_and_keeps_the_rest():
    table = cr.load({"github": ENTRY, "bad": {**ENTRY, "password": "plain"}, "worse": "x"})
    assert set(table.by_site) == {"github"}
    assert len(table.problems) == 2
    assert any(p.startswith("bad:") for p in table.problems)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/login",
        "https://GitHub.com/session?x=1#y",
        "https://github.com:443/",
        "https://github.com./login",
    ],
)
def test_check_origin_accepts_the_same_origin(url):
    cr.check_origin(cred(), url)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/login",
        "https://github.com.evil.com/login",
        "https://evil.com/github.com",
        "https://login.github.com/",
        "https://github.com:8443/",
        "https://github.com@evil.com/",
        "https://evil.com\\@github.com/",
        "https://evil.com#@github.com",
        "about:blank",
        "",
        "https://[::1",
    ],
)
def test_check_origin_refuses_every_other_origin(url):
    with pytest.raises(cr.OriginMismatch):
        cr.check_origin(cred(), url)


def test_check_origin_compares_idna_hosts():
    c = cred(origin="https://münchen.example")
    cr.check_origin(c, "https://xn--mnchen-3ya.example/")
    with pytest.raises(cr.OriginMismatch):
        cr.check_origin(c, "https://munchen.example/")


def test_mismatch_message_names_the_origins_not_the_references():
    with pytest.raises(cr.OriginMismatch) as err:
        cr.check_origin(cred(), "https://evil.com/")
    assert "https://github.com" in str(err.value)
    assert "https://evil.com" in str(err.value)
    assert "op://" not in str(err.value)


def test_config_reads_the_global_file_only(tmp_path, monkeypatch):
    glob = tmp_path / "global.toml"
    monkeypatch.setattr(config, "_global_path", lambda: glob)
    glob.write_text(
        '[browser.login.github]\norigin = "https://github.com"\n'
        'username = "op://V/I/u"\npassword = "op://V/I/p"\n'
    )
    repo = tmp_path / "repo" / ".cabildo"
    repo.mkdir(parents=True)
    (repo / "config.toml").write_text(
        '[browser.login.evil]\norigin = "https://evil.com"\n'
        'username = "op://V/I/u"\npassword = "op://V/I/p"\n'
    )
    assert set(config.browser_login_tables()) == {"github"}


def test_config_without_the_table_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_global_path", lambda: tmp_path / "missing.toml")
    assert config.browser_login_tables() == {}
