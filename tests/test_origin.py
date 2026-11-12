"""backend/origin.py: cross-site request guard."""
from __future__ import annotations

import socket

import pytest

from backend import origin as origin_mod
from backend.origin import UNSAFE_METHODS, cross_site, host_allowed


@pytest.fixture(autouse=True)
def _box(monkeypatch):
    """A known machine name, and no allow-list leaking in from the shell."""
    monkeypatch.setattr(socket, "gethostname", lambda: "grid")
    monkeypatch.delenv(origin_mod.ALLOWED_HOSTS_ENV, raising=False)


@pytest.mark.parametrize(
    "origin,host",
    [
        (None, "ticker.local:8080"),  # curl, scripts
        ("", "ticker.local:8080"),
        ("http://ticker.local:8080", "ticker.local:8080"),  # the admin itself
        ("http://192.168.1.50:8080", "192.168.1.50:8080"),
        ("http://Ticker.Local:8080", "ticker.local:8080"),  # case-insensitive
        ("http://[fe80::1]:8080", "[fe80::1]:8080"),
        ("http://127.0.0.1:8080", "127.0.0.1:8080"),  # the kiosk
        ("http://localhost:8080", "localhost:8080"),
        ("http://grid:8080", "grid:8080"),  # the box's own (single-label) name
        ("http://grid.lan:8080", "grid.lan:8080"),
        ("http://ticker.home.arpa", "ticker.home.arpa"),
        ("http://ticker.internal:8080", "ticker.internal:8080"),
        ("http://grid.localdomain:8080", "grid.localdomain:8080"),
        ("http://ticker.local.:8080", "ticker.local.:8080"),  # FQDN trailing dot
        # No Origin = not a browser page's request; rebinding always carries one.
        (None, "evil.example:8080"),
    ],
)
def test_same_site_or_no_origin_passes(origin, host):
    assert cross_site(origin, host) is False


@pytest.mark.parametrize(
    "origin,host",
    [
        ("null", "ticker.local:8080"),  # sandboxed iframe / file://
        ("https://evil.example", "ticker.local:8080"),
        ("http://ticker.local:5173", "ticker.local:8080"),  # a different port is a different origin
        ("http://ticker.local", "ticker.local:8080"),
        ("http://ticker.local:8080", None),
        ("http://ticker.local.evil.example:8080", "ticker.local:8080"),
    ],
)
def test_cross_site_is_refused(origin, host):
    assert cross_site(origin, host) is True


@pytest.mark.parametrize(
    "name",
    [
        "evil.example:8080",
        "evil.example",
        "192-168-1-50.nip.io:8080",  # public wildcard DNS that answers with a LAN address
        "ticker.local.evil.example:8080",
        "grid.example.com:8080",  # the box's short name under someone else's domain
    ],
)
def test_dns_rebinding_is_refused(name):
    """Origin and Host agree, but the name is a public one an attacker can point
    at the appliance's LAN address after the page has loaded."""
    assert cross_site(f"http://{name}", name) is True


@pytest.mark.parametrize(
    "host", ["x:abc", "a@127.0.0.1", "fe80::1", "ticker.local:8080/x", "[fe80::1", "", None]
)
def test_malformed_hosts_are_not_allowed(host):
    assert host_allowed(host) is False


def test_the_machines_own_hostname_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Kiosk.Example.com")
    assert cross_site("http://kiosk.example.com:8080", "kiosk.example.com:8080") is False
    assert cross_site("http://other.example.com:8080", "other.example.com:8080") is True


def test_env_allow_list(monkeypatch):
    monkeypatch.setenv(origin_mod.ALLOWED_HOSTS_ENV, " ticker.example.com, Edge.Example.net:8443 ,,")
    assert cross_site("http://ticker.example.com:8080", "ticker.example.com:8080") is False
    assert cross_site("https://edge.example.net", "edge.example.net") is False  # port ignored
    assert cross_site("http://evil.example:8080", "evil.example:8080") is True
    # an allowed Host still has to match the Origin
    assert cross_site("http://evil.example:8080", "ticker.example.com:8080") is True


def test_unsafe_methods():
    assert UNSAFE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}
