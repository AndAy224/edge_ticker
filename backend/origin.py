"""Cross-site request guard for the LAN-trust endpoints.

The admin API has no login by design (PLAN.md: LAN trust), but "anyone on the
LAN" must not stretch to "any web page a LAN user happens to open". Browsers
send an Origin header on cross-site POST/PUT and on every WebSocket handshake,
and the handlers parse JSON bodies regardless of Content-Type, so a page could
fire a `text/plain` POST with no CORS preflight at all. A request whose Origin
names a different host than it was sent to is refused. No Origin (curl,
scripts, the kiosk's own same-origin fetches) passes.
"""
from __future__ import annotations

from urllib.parse import urlsplit

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def cross_site(origin: str | None, host: str | None) -> bool:
    if not origin:
        return False
    if origin == "null":  # sandboxed iframes, file:// pages
        return True
    return urlsplit(origin).netloc.lower() != (host or "").lower()
