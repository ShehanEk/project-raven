"""Wake-on-LAN magic packets."""

from __future__ import annotations

import socket

from raven.platform.netinfo import broadcast_for_address, normalize_mac


def magic_packet(mac: str) -> bytes:
    normalized = normalize_mac(mac)
    if not normalized:
        raise ValueError(f"invalid MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(normalized.replace(":", "")) * 16


def wake(mac: str, target_ip: str | None = None) -> None:
    """Broadcast a magic packet on the local network.

    Sent to the limited broadcast address and, when the target's subnet is
    known, to that subnet's directed broadcast too, on both common ports.
    """
    packet = magic_packet(mac)
    destinations = {"255.255.255.255"}
    if target_ip:
        directed = broadcast_for_address(target_ip)
        if directed:
            destinations.add(directed)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for dest in destinations:
            for port in (9, 7):
                try:
                    s.sendto(packet, (dest, port))
                except OSError:
                    pass
