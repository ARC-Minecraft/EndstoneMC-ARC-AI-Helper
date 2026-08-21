"""Extract connection IP from an Endstone player."""

from __future__ import annotations

from typing import Any


def extract_player_ip(player: Any) -> str:
    """Return IPv4/IPv6 text from an Endstone player, or empty."""
    if player is None:
        return ""
    address = getattr(player, "address", None)
    if address is None:
        return ""
    host = str(getattr(address, "hostname", "") or "").strip()
    if not host:
        host = str(address).strip()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    # Sometimes str(address) is "1.2.3.4:19132"
    if ":" in host and host.count(":") == 1 and not host.startswith(":"):
        left, right = host.rsplit(":", 1)
        if right.isdigit():
            host = left
    return host.strip()
