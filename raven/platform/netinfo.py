"""Local network details: addresses to show the user and the MAC for Wake-on-LAN."""

from __future__ import annotations

import ipaddress
import re
import socket

import psutil

from raven.security import is_lan_address


def primary_ipv4() -> str | None:
    """The address the OS would use to reach the LAN/default route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # No packet is sent; this only selects a route.
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_ipv4_addresses() -> list[str]:
    primary = primary_ipv4()
    found = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and is_lan_address(a.address) and not a.address.startswith("127."):
                found.append(a.address)
    found.sort(key=lambda ip: ip != primary)
    return found


def mac_for_address(ip: str | None) -> str:
    """MAC address of the interface carrying ``ip`` (empty if unknown)."""
    if not ip:
        return ""
    for addrs in psutil.net_if_addrs().values():
        if any(a.family == socket.AF_INET and a.address == ip for a in addrs):
            for a in addrs:
                if a.family == psutil.AF_LINK and a.address:
                    mac = normalize_mac(a.address)
                    if mac and mac != "00:00:00:00:00:00":
                        return mac
    return ""


def broadcast_for_address(ip: str) -> str | None:
    """Directed broadcast address of the local subnet that contains ``ip``."""
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
            try:
                if ipaddress.ip_address(ip) in net:
                    return str(net.broadcast_address)
            except ValueError:
                return None
    return None


_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(mac: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    if not _MAC_RE.match(digits):
        return ""
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))
