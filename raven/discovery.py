"""Find Raven hosts on the LAN with mDNS / Bonjour."""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncZeroconf

from raven import PROTOCOL_VERSION, SERVICE_TYPE
from raven.config import local_os
from raven.platform import netinfo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredHost:
    service_name: str
    name: str
    address: str
    port: int
    os: str
    fingerprint: str


class Advertiser:
    """Publishes this host on the LAN. Use from an asyncio event loop."""

    def __init__(self):
        self._azc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    async def start(self, name: str, port: int, fingerprint: str) -> None:
        await self.stop()
        addresses = netinfo.lan_ipv4_addresses()
        if not addresses:
            log.info("no LAN address; not advertising")
            return
        safe_name = "".join(c for c in name if c.isprintable() and c != ".")[:50] or "Raven host"
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{safe_name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in addresses],
            port=port,
            properties={"name": name, "os": local_os(), "fp": fingerprint, "v": str(PROTOCOL_VERSION)},
            server=f"{socket.gethostname().split('.')[0]}.local.",
        )
        self._azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        try:
            await self._azc.async_register_service(self._info, allow_name_change=True)
        except Exception:  # noqa: BLE001 - discovery is a convenience
            log.exception("mDNS registration failed")

    async def stop(self) -> None:
        azc, self._azc = self._azc, None
        if azc:
            try:
                if self._info:
                    await azc.async_unregister_service(self._info)
            except Exception:  # noqa: BLE001
                pass
            await azc.async_close()


class Browser:
    """Watches for hosts. Callbacks run on a zeroconf thread."""

    def __init__(self, on_found: Callable[[DiscoveredHost], None], on_lost: Callable[[str], None]):
        self.on_found = on_found
        self.on_lost = on_lost
        self._zc = Zeroconf(ip_version=IPVersion.V4Only)
        self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, handlers=[self._on_change])

    def _on_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange):
        if state_change is ServiceStateChange.Removed:
            self.on_lost(name)
            return
        info = zeroconf.get_service_info(service_type, name, timeout=3000)
        if not info:
            return
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if not addresses:
            return
        props = {k.decode(): (v or b"").decode(errors="replace") for k, v in info.properties.items()}
        self.on_found(DiscoveredHost(
            service_name=name,
            name=props.get("name") or name.removesuffix("." + SERVICE_TYPE),
            address=addresses[0],
            port=info.port,
            os=props.get("os", ""),
            fingerprint=props.get("fp", ""),
        ))

    def close(self) -> None:
        self._browser.cancel()
        self._zc.close()
