"""Host facts for the display. Currently just the LAN address the admin GUI
is reachable at, shown on the HA overlay so the panel can tell you where it is."""
from __future__ import annotations

import socket
import time

_CACHE_TTL = 60.0
_cached: tuple[float, str | None] = (0.0, None)


def lan_ip() -> str | None:
    """The source address of the default route, or None if there is no route.

    UDP `connect()` sends no packets — it only asks the kernel which local
    address it would use, which is the one a phone on the LAN can reach.
    Cached briefly so a reconnect storm doesn't re-syscall, while still
    picking up a DHCP change within a minute.
    """
    global _cached
    now = time.monotonic()
    if now - _cached[0] < _CACHE_TTL:
        return _cached[1]

    ip: str | None = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        ip = None
    finally:
        sock.close()

    _cached = (now, ip)
    return ip
