"""On-demand detail proxies (backend/api/): an upstream 200 whose body isn't the
JSON they expect must take the error path, not escape as an uncached 500."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.responses import JSONResponse

from backend.api import adsb, fantasy, markets, sports

HTML = "<html><body>502 Bad Gateway</body></html>"


def html_page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    for module in (adsb, markets, sports, fantasy):
        monkeypatch.setattr(module, "_cache", {})
    monkeypatch.setattr(fantasy, "_player_cache", {})


def error_status(result) -> int | None:
    return result.status_code if isinstance(result, JSONResponse) else None


# --- adsb: its error path is a short-lived {"route": null} -------------------

@pytest.mark.parametrize("body", [HTML, "[]", '"maintenance"', '{"response": "unknown callsign"}'])
async def test_adsb_route_odd_body_takes_the_error_path(http, body):
    http.handler = lambda request: httpx.Response(200, text=body)
    assert await adsb.adsb_route("DAL123") == {"route": None}
    if body == HTML:  # undecodable: retried soon, like a timeout
        assert adsb._cache["DAL123"][1] == adsb.ERROR_TTL_SECONDS


async def test_adsb_route_still_resolves_a_route(http):
    http.handler = lambda request: httpx.Response(200, json={"response": {"flightroute": {
        "origin": {"iata_code": "TPA"}, "destination": {"iata_code": "ATL"},
        "airline": {"name": "Delta"},
    }}})
    route = (await adsb.adsb_route("dal123"))["route"]
    assert route["origin"]["iata"] == "TPA" and route["destination"]["iata"] == "ATL"
    assert route["airline"] == "Delta"


# --- markets ----------------------------------------------------------------

@pytest.fixture
def finnhub_key(monkeypatch):
    monkeypatch.setenv("FINNHUB_KEY", "test-key")


async def test_markets_equity_garbage_everywhere_is_an_uncached_502(http, finnhub_key):
    http.handler = html_page
    assert error_status(await markets.market_detail("AAPL")) == 502
    assert "AAPL" not in markets._cache
    sent = len(http.requests)
    assert error_status(await markets.market_detail("AAPL")) == 502
    assert len(http.requests) == 2 * sent  # the next tap asks again


async def test_markets_equity_one_garbage_section_drops_only_that_section(http, finnhub_key):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/stock/profile2"):
            return html_page(request)
        if path.endswith("/stock/metric"):
            return httpx.Response(200, json={"metric": {"52WeekHigh": 250.0, "peTTM": 31.2}})
        if path.endswith("/company-news"):
            return httpx.Response(200, json=[{"headline": "Apple ships", "datetime": 1}])
        if path.endswith("/calendar/earnings"):
            return httpx.Response(200, json={"earningsCalendar": []})
        return httpx.Response(200, json=[{"buy": 3, "hold": 1}])

    http.handler = handler
    detail = await markets.market_detail("AAPL")
    assert isinstance(detail, dict)
    assert detail["profile"] is None
    assert detail["metrics"]["high52"] == 250.0
    assert [n["headline"] for n in detail["news"]] == ["Apple ships"]
    assert detail["recommendation"]["label"] == "Buy"
    assert markets._cache["AAPL"][1] is detail


async def test_markets_crypto_garbage_is_an_uncached_502(http, finnhub_key):
    http.handler = html_page
    assert error_status(await markets.market_detail("BTC-USD")) == 502
    assert "BTC-USD" not in markets._cache


async def test_markets_crypto_news(http, finnhub_key):
    http.handler = lambda request: httpx.Response(200, json=[
        {"headline": "Bitcoin rallies", "summary": "", "datetime": 2},
        {"headline": "Ether slips", "summary": "", "datetime": 1},
    ])
    detail = await markets.market_detail("BTC-USD")
    assert detail["profile"] == {"name": "Bitcoin"}
    assert [n["headline"] for n in detail["news"]] == ["Bitcoin rallies"]


# --- sports / fantasy: already 502 on undecodable bodies; also on non-objects --

@pytest.mark.parametrize("body", [HTML, "[]", "null"])
async def test_sports_detail_odd_body_is_an_uncached_502(http, body):
    http.handler = lambda request: httpx.Response(200, text=body)
    result = await sports.game_detail("football", "nfl", "401")
    assert error_status(result) == 502
    assert json.loads(result.body)["error"].startswith("summary fetch failed")
    assert sports._cache == {}


@pytest.mark.parametrize("body", [HTML, "[]", "null"])
async def test_fantasy_detail_odd_body_is_an_uncached_502(http, monkeypatch, body):
    async def pro_teams(season):  # the pro schedule fetch is not under test
        return {}

    monkeypatch.setattr(fantasy, "pro_teams", pro_teams)
    http.handler = lambda request: httpx.Response(200, text=body)
    result = await fantasy.fantasy_detail("123", 2026, 8, 1)
    assert error_status(result) == 502
    assert fantasy._cache == {}
