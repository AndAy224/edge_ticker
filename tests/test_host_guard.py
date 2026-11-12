"""The app-wide Host check in backend/main.py's middleware (DNS rebinding)."""
from __future__ import annotations

import httpx
import pytest

from backend.main import app


async def get(host: str, path: str = "/api/no-such-route", method: str = "GET", **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ticker") as client:
        return await client.request(method, path, headers={"Host": host, **kwargs.pop("headers", {})},
                                    **kwargs)


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8080", "10.0.0.5.nip.io"])
async def test_reads_addressed_to_a_public_name_are_refused(host):
    # A rebinding page's same-origin GETs carry no Origin: checking only writes
    # left /api/config and the camera proxies readable.
    r = await get(host)
    assert r.status_code == 403
    assert "TICKER_ALLOWED_HOSTS" in r.json()["error"]


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "192.168.1.50:8080", "[::1]:8080",
                                  "grid", "grid:8080", "ticker.local", "edge.lan:8080",
                                  "ticker.home", "localhost:8080"])
async def test_lan_names_pass_the_guard(host):
    r = await get(host)
    assert r.status_code == 404  # reached routing


async def test_listed_names_pass(monkeypatch):
    monkeypatch.setenv("TICKER_ALLOWED_HOSTS", "ticker.example.com, grid.tail1234.ts.net")
    assert (await get("grid.tail1234.ts.net")).status_code == 404
    assert (await get("ticker.example.com:443")).status_code == 404


async def test_cross_site_writes_are_still_refused():
    r = await get("192.168.1.50:8080", method="POST",
                  headers={"Origin": "http://elsewhere.lan:8080"})
    assert r.status_code == 403
    assert r.json()["error"] == "cross-site request refused"
