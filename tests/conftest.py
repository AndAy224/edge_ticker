"""Shared test plumbing. The suite runs fully offline.

Fixture provenance (tests/fixtures/, recorded once on 2026-09-25, trimmed):
  open_meteo_forecast.json      api.open-meteo.com/v1/forecast (weather collector params, Clearwater)
  open_meteo_air_quality.json   air-quality-api.open-meteo.com/v1/air-quality (airquality params)
  rainviewer_weather_maps.json  api.rainviewer.com/public/weather-maps.json
  nhc_current_storms.json       www.nhc.noaa.gov/CurrentStorms.json (2 Atlantic + 3 East Pacific)
  adsblol_clearwater.json       api.adsb.lol/v2/lat/27.9659/lon/-82.8001/dist/22
  nws_alerts_point.json         api.weather.gov/alerts/active?point=27.9659,-82.8001 (no alerts)
  nws_alerts_tx.json            api.weather.gov/alerts/active?area=TX, trimmed to 3 features
  espn_{nfl,mlb}_YYYYMMDD.json  ESPN site scoreboard ?dates=YYYYMMDD, 1-3 events, unused keys dropped
  ll2_upcoming.json             Launch Library 2 launch/upcoming (one request), 4 launches, unused keys dropped
  bbc_world.xml                 feeds.bbci.co.uk/news/world/rss.xml, first 5 items

  opnsense_*.json               OPNsense 26.7 API on the home firewall, 2026-09-26: routes/gateway/status,
                                routing/settings/searchGateway, diagnostics/interface/getRoutes,
                                diagnostics/traffic/interface (twice, ~16 s apart: _2),
                                interfaces/overview/interfacesInfo (per-row config blobs dropped), and
                                diagnostics/system/{systemTime,systemResources,systemInformation},
                                diagnostics/firewall/pf_states. Public IPs rewritten to 203.0.113.x /
                                198.51.100.x, MACs to 00:00:5e:00:53:xx. WAN up; Starlink port with no
                                carrier (its gateway Offline, no address).

Do not re-record casually: Launch Library 2 allows ~15 req/hr per IP and the
production appliance shares that budget.
"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

# Before any backend import: backend.db (and stocks, via DB_PATH) resolve the
# data directory at import time — keep them off the dev checkout's data/.
os.environ["TICKER_DB"] = str(Path(tempfile.mkdtemp(prefix="edge-ticker-tests-")) / "ticker.db")

import httpx  # noqa: E402
import pytest  # noqa: E402

from helpers import FakeBus, HttpMock  # noqa: E402

# Env the collectors/bridge read. A developer shell (or a prior import of
# backend.main, which loads .env) must not leak real credentials into tests.
SCRUBBED_ENV = (
    "FINNHUB_KEY", "PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET", "PVE_VERIFY_SSL",
    "UNIFI_URL", "UNIFI_API_KEY", "UNIFI_VERIFY_SSL", "HA_URL", "HA_TOKEN",
    "ESPN_S2", "ESPN_SWID", "ADSB_URL",
    "OPNSENSE_URL", "OPNSENSE_KEY", "OPNSENSE_SECRET", "OPNSENSE_VERIFY_SSL",
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No test may reach the network: any real TCP connect or DNS lookup fails."""

    def refuse(*args, **kwargs):
        raise RuntimeError("network access attempted in an offline test")

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse()
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def http(monkeypatch) -> HttpMock:
    """Every httpx.AsyncClient built during the test talks to `http.handler`."""
    mock = HttpMock()
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        mock.client_kwargs.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(mock)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return mock


@pytest.fixture
def defaults_config() -> dict:
    """A fresh copy of config/defaults.yaml."""
    from backend import db

    return db.defaults()


@pytest.fixture
def fresh_overlay_state(monkeypatch):
    """weather_alerts keeps its takeover dedup state at module level (on purpose:
    it must survive collector restarts) — isolate it per test."""
    from backend.collectors import weather_alerts

    monkeypatch.setattr(weather_alerts, "_fired_ids", {})
    monkeypatch.setattr(weather_alerts, "_event_fired_at", {})


@pytest.fixture(autouse=True)
def _fresh_opnsense_state(monkeypatch):
    """opnsense keeps counters, sparkline history and the failover clock at
    module level (they must survive collector restarts) — isolate per test."""
    from backend.collectors import opnsense

    monkeypatch.setattr(opnsense, "_counters", {})
    monkeypatch.setattr(opnsense, "_history", {})
    monkeypatch.setattr(opnsense, "_active", {"seen": False, "id": None, "since": None})
