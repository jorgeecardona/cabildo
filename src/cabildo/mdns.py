"""mDNS / DNS-SD for the LAN service: announce this machine, find the others.

Each daemon registers ``<node>._cabildo._tcp.local.`` with its current TCP port
and two TXT properties: ``node`` and ``pairing`` (``"1"`` while a pairing window
is open). A browser keeps a table of every cabildo machine it hears.

This is only discovery. Nothing here is trusted: a paired connection still
checks the pinned certificate, so a forged announcement can at worst point a
peer at a wrong address, where the TLS check fails.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

SERVICE_TYPE = "_cabildo._tcp.local."
_RESOLVE_MS = 3000


@dataclass(frozen=True)
class Seen:
    """One cabildo machine heard on the network."""

    node: str
    hosts: tuple[str, ...]
    port: int
    pairing: bool
    at: float
    # The registered service name; differs from ``<node>.<type>`` after a rename.
    name: str = ""


class Mdns:
    """Announce this node and browse for the others. One per daemon."""

    def __init__(
        self,
        node: str,
        port: int,
        on_seen: Callable[[Seen], None] | None = None,
        zc: Zeroconf | None = None,
    ) -> None:
        self.node = node
        self.port = port
        self.on_seen = on_seen
        self.zc = zc or Zeroconf(ip_version=IPVersion.V4Only)
        self._lock = threading.Lock()
        self._seen: dict[str, Seen] = {}
        self._info = self._make_info(pairing=False)
        self.zc.register_service(self._info, allow_name_change=True)
        self._browser = ServiceBrowser(self.zc, SERVICE_TYPE, handlers=[self._on_change])

    def _make_info(self, *, pairing: bool, name: str | None = None) -> ServiceInfo:
        return ServiceInfo(
            SERVICE_TYPE,
            name or f"{self.node}.{SERVICE_TYPE}",
            port=self.port,
            properties={"node": self.node, "pairing": "1" if pairing else "0"},
            server=f"cabildo-{self.node[:12]}.local.",
            parsed_addresses=local_addresses(),
        )

    def set_pairing(self, pairing: bool) -> None:
        """Tell the network whether a pairing window is open here."""
        # Keep the registered name: register_service may have renamed it.
        self._info = self._make_info(pairing=pairing, name=self._info.name)
        self.zc.update_service(self._info)

    def seen(self) -> list[Seen]:
        with self._lock:
            return list(self._seen.values())

    def pairing_peers(self) -> list[Seen]:
        """Other machines that have a pairing window open right now.

        Re-reads each machine's TXT record from the zeroconf cache: the browser
        does not fire an update for every TXT change, so the table alone can be
        stale.
        """
        out: list[Seen] = []
        for s in self.seen():
            info = ServiceInfo(SERVICE_TYPE, s.name)
            # A cache miss (mid-update) reads as "not pairing"; callers poll.
            loaded = info.load_from_cache(self.zc)
            if loaded and info.decoded_properties.get("pairing") == "1" and s.node != self.node:
                out.append(s)
        return out

    def close(self) -> None:
        self._browser.cancel()
        self.zc.unregister_all_services()
        self.zc.close()

    def _on_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        # Called on zeroconf's own thread, which must not block: resolve elsewhere.
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                self._seen.pop(name, None)
            return
        threading.Thread(
            target=self._resolve, args=(zeroconf, service_type, name), daemon=True
        ).start()

    def _resolve(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = zeroconf.get_service_info(service_type, name, timeout=_RESOLVE_MS)
        if info is None or not info.port:
            return
        props: dict[Any, Any] = info.decoded_properties
        node = str(props.get("node") or "")
        hosts = info.parsed_addresses(IPVersion.V4Only)
        if not node or node == self.node or not hosts:
            return
        s = Seen(
            node=node,
            hosts=tuple(hosts),
            port=int(info.port),
            pairing=props.get("pairing") == "1",
            at=time.time(),
            name=name,
        )
        # Keyed by service name, not node id: a forged announcement that copies a
        # node id is a second entry, so "exactly one machine pairing" catches it.
        with self._lock:
            self._seen[name] = s
        if self.on_seen is not None:
            self.on_seen(s)


def local_addresses() -> list[str]:
    """Every non-loopback IPv4 address of this machine, so a peer on any LAN finds
    one it can reach (a VPN or docker address alone would not do).
    """
    import ifaddr

    out = [
        str(ip.ip)
        for adapter in ifaddr.get_adapters()
        for ip in adapter.ips
        if isinstance(ip.ip, str) and not ip.ip.startswith("127.")
    ]
    return out or ["127.0.0.1"]
