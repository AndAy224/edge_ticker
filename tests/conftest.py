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
