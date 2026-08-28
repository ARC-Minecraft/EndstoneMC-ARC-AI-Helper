"""Extract connection IP from an Endstone player."""

from __future__ import annotations

from typing import Any


def _looks_like_ip(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if s.startswith("[") and "]" in s:
        s = s[1 : s.index("]")]
    if "." in s:
        left = s.split(":", 1)[0]
        parts = left.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            return True
    if ":" in s and s.count(":") >= 2:
        return True
    return False


def extract_player_ip(player: Any) -> str:
    """Return IPv4/IPv6 text from an Endstone player, or empty.

    Prefer raw IP fields and never call ``hostname`` first — that can trigger
    blocking reverse DNS (getnameinfo) on the server thread.
    """
    if player is None:
        return ""
    address = getattr(player, "address", None)
    if address is None:
        return ""
    host = ""
    for attr in ("address", "ip", "host"):
        raw = str(getattr(address, attr, "") or "").strip()
        if raw:
            host = raw
            break
    if not host:
        as_text = str(address).strip()
        if _looks_like_ip(as_text):
            host = as_text
    if not host:
        host = str(getattr(address, "hostname", "") or "").strip()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    # Sometimes str(address) is "1.2.3.4:19132"
    if ":" in host and host.count(":") == 1 and not host.startswith(":"):
        left, right = host.rsplit(":", 1)
        if right.isdigit():
            host = left
    return host.strip()
