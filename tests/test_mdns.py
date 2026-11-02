"""mDNS discovery (cabildo.mdns): two announcers on this host see each other."""

from __future__ import annotations

import time

import pytest

from cabildo import mdns


def _wait(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


TEST_PORTS = range(40001, 40007)


def _ours(seen):
    """Only the announcers of this test file: a real machine on the LAN can be pairing too."""
    return [s for s in seen if s.port in TEST_PORTS]


def _mdns(node: str, port: int) -> mdns.Mdns:
    try:
        return mdns.Mdns(node, port)
    except OSError as e:
        pytest.skip(f"no multicast on this host: {e}")


def test_a_pairing_window_is_seen_and_then_cleared():
    a = _mdns("a" * 32, 40001)
    b = _mdns("b" * 32, 40002)
    try:
        if not _wait(lambda: any(s.node == "a" * 32 for s in b.seen())):
            pytest.skip("no multicast on this host")
        assert _ours(b.pairing_peers()) == []
        a.set_pairing(True)
        assert _wait(lambda: [s.port for s in _ours(b.pairing_peers())] == [40001])
        time.sleep(2.0)  # mDNS sends one change of a record per second (RFC 6762)
        a.set_pairing(False)
        assert _wait(lambda: _ours(b.pairing_peers()) == [])
    finally:
        a.close()
        b.close()


def test_a_machine_never_lists_itself():
    a = _mdns("c" * 32, 40003)
    try:
        time.sleep(1.0)
        assert all(s.node != "c" * 32 for s in a.seen())
    finally:
        a.close()


def test_a_forged_copy_of_a_pairing_node_is_a_second_entry():
    real = _mdns("d" * 32, 40004)
    fake = _mdns("e" * 32, 40005)
    b = _mdns("f" * 32, 40006)
    try:
        if not _wait(lambda: len(_ours(b.seen())) >= 2):
            pytest.skip("no multicast on this host")
        # The forger announces the real node id under its own service name.
        fake.node = "d" * 32
        real.set_pairing(True)
        time.sleep(1.1)
        fake.set_pairing(True)
        assert _wait(lambda: len(_ours(b.pairing_peers())) == 2)
    finally:
        for m in (real, fake, b):
            m.close()
