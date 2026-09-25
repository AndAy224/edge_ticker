"""backend/origin.py: cross-site request guard."""
from __future__ import annotations

import pytest

from backend.origin import UNSAFE_METHODS, cross_site


@pytest.mark.parametrize(
    "origin,host",
    [
        (None, "ticker.local:8080"),  # curl, scripts
        ("", "ticker.local:8080"),
        ("http://ticker.local:8080", "ticker.local:8080"),  # the admin itself
        ("http://192.168.1.50:8080", "192.168.1.50:8080"),
        ("http://Ticker.Local:8080", "ticker.local:8080"),  # case-insensitive
        ("http://[fe80::1]:8080", "[fe80::1]:8080"),
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


def test_unsafe_methods():
    assert UNSAFE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}
