"""Config-driven, mutual project linking: a project names its friends in its own
`.cabildo/config.toml`, and two projects see each other on the bus only when each
names the other (or one is `[link].open`). This is the declarative, isolation-
preserving alternative to the symmetric `cabildo project --link` (cloud.json).

The invariant pinned here is the consent one: no project can join another's bus
by editing only its own file — the other side must name it back.
"""

from __future__ import annotations

import pytest

from cabildo import cloud


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    # cloud.json lives at ~/.cabildo/cloud.json (real HOME, not CABILDO_HOME), so
    # every cloud test must redirect HOME or it writes the user's own registry.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # _link_cfg/project_key cache by NAME with a short TTL; across tests the same
    # name maps to different roots, so a stale entry would leak between them.
    cloud._link_cache.clear()
    cloud._key_cache.clear()
    yield
    cloud._link_cache.clear()
    cloud._key_cache.clear()


def _mkproj(tmp_path, name, toml):
    d = tmp_path / name
    (d / ".cabildo").mkdir(parents=True, exist_ok=True)
    (d / ".cabildo" / "config.toml").write_text(toml)
    cloud.register_root(name, str(d))
    return str(d)


def test_mutual_friends_see_each_other(tmp_path):
    _mkproj(tmp_path, "a", '[link]\nfriends = ["b"]\n')
    _mkproj(tmp_path, "b", '[link]\nfriends = ["a"]\n')
    assert cloud.peers_visible("a") == {"a", "b"}
    assert cloud.peers_visible("b") == {"a", "b"}


def test_one_sided_friend_is_not_a_link(tmp_path):
    _mkproj(tmp_path, "a", '[link]\nfriends = ["b"]\n')
    _mkproj(tmp_path, "b", "")  # b never names a back
    assert cloud.peers_visible("a") == {"a"}
    assert cloud.peers_visible("b") == {"b"}


def test_open_project_is_reachable_by_everyone(tmp_path):
    _mkproj(tmp_path, "a", '[link]\nfriends = ["d"]\n')
    _mkproj(tmp_path, "d", "[link]\nopen = true\n")
    # a reaches d because d is open — even though d never named a.
    assert "d" in cloud.peers_visible("a")


def test_open_still_needs_consent_to_reach_out(tmp_path):
    # Open means "others may reach me", not "I see everyone": d reaching a still
    # needs a to name d (or be open). a does not, so d's set is just itself.
    _mkproj(tmp_path, "a", "")
    _mkproj(tmp_path, "d", "[link]\nopen = true\n")
    assert cloud.peers_visible("d") == {"d"}
    assert "d" in cloud.peers_visible("a")  # a may reach open d


def test_two_open_projects_link_automatically(tmp_path):
    _mkproj(tmp_path, "x", "[link]\nopen = true\n")
    _mkproj(tmp_path, "y", "[link]\nopen = true\n")
    assert cloud.peers_visible("x") == {"x", "y"}
    assert cloud.peers_visible("y") == {"x", "y"}


def test_isolated_by_default(tmp_path):
    _mkproj(tmp_path, "a", "")
    _mkproj(tmp_path, "b", "")
    assert cloud.peers_visible("a") == {"a"}


def test_legacy_cloud_json_link_still_works(tmp_path):
    # A project with no [link] config but an old symmetric --link peer stays linked.
    _mkproj(tmp_path, "a", "")
    _mkproj(tmp_path, "b", "")
    cloud.add_peer("a", "b")
    assert cloud.peers_visible("a") == {"a", "b"}
    assert cloud.peers_visible("b") == {"a", "b"}
