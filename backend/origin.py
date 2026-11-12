"""Cross-site request guard for the LAN-trust endpoints.

The admin API has no login by design (PLAN.md: LAN trust), but "anyone on the
LAN" must not stretch to "any web page a LAN user happens to open". Browsers
send an Origin header on cross-site POST/PUT and on every WebSocket handshake,
and the handlers parse JSON bodies regardless of Content-Type, so a page could
fire a `text/plain` POST with no CORS preflight at all. A request whose Origin
names a different host than it was sent to is refused.

Comparing Origin with Host alone let DNS rebinding through: a page on
`evil.example:8080` whose name is then re-pointed at the appliance's LAN
address sends `Origin: http://evil.example:8080` *and* `Host:
evil.example:8080` — they match, and the browser hands that page config PUTs,
HA control and the whole WS stream. So a request that carries an Origin must
also be addressed to a name the appliance plausibly answers to on the LAN (see
`host_allowed`); the attacker needs a public DNS name it controls, and those
are refused unless listed in TICKER_ALLOWED_HOSTS.

No Origin still passes, with no Host check: rebinding is a browser attack, and
browsers send Origin on every request that isn't a GET/HEAD and on every
WebSocket handshake, so an unsafe request or WS from a hostile page always
carries one. Clients without it (curl, scripts, HA's rest_command) run no
foreign page's code and could put any Host they liked on the request anyway.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Names that only resolve inside a LAN (mDNS, router DNS, RFC 8375/6762/6761
# special-use) — a rebinding attacker can't register them publicly.
LAN_SUFFIXES = (
    ".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".localhost",
    # never delegated (ICANN deferred them over name collisions) and common as
    # home-router domains, so just as unregistrable for an attacker
    ".home", ".corp",
)
ALLOWED_HOSTS_ENV = "TICKER_ALLOWED_HOSTS"


def _host_name(host: str | None) -> str | None:
    """`name[:port]` / `[v6]:port` → the bare lowercase name, None if malformed."""
    if not host:
        return None
    try:
        parts = urlsplit("//" + host.strip())
        name = parts.hostname
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if not name or parts.username is not None or parts.path or parts.query or parts.fragment:
        return None
    return name.rstrip(".") or None


def _own_names() -> set[str]:
    # gethostname() only, not getfqdn(): that is a blocking reverse-DNS lookup
    # on the request path. The FQDN is normally under .lan/.local/.localdomain
    # anyway; a box named under a real domain goes in TICKER_ALLOWED_HOSTS.
    name = socket.gethostname().lower().rstrip(".")
    return {name, name.split(".")[0]} if name else set()


def _env_hosts() -> set[str]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return {n for n in (_host_name(entry) for entry in raw.split(",")) if n}


def host_allowed(host: str | None) -> bool:
    """Whether `host` (a Host header) names this appliance as LAN clients reach
    it: an IP literal, localhost or another single-label name (the box is
    `grid`), a LAN-only suffix, the machine's own hostname, or an entry of
    TICKER_ALLOWED_HOSTS (comma-separated, ports ignored)."""
    name = _host_name(host)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name or name.endswith(LAN_SUFFIXES):
        return True
    return name in _own_names() or name in _env_hosts()


def cross_site(origin: str | None, host: str | None) -> bool:
    """True when a request with this Origin/Host must be refused."""
    if not origin:
        return False
    if origin == "null":  # sandboxed iframes, file:// pages
        return True
    if not host_allowed(host):
        return True  # DNS rebinding: Origin and Host agree on a name that isn't ours
    return urlsplit(origin).netloc.lower() != (host or "").lower()
