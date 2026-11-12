"""On-demand detail proxies (backend/api/): an upstream 200 whose body isn't the
JSON they expect must take the error path, not escape as an uncached 500; and
what a caller can make them ask upstream — which path, how often — is bounded."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from backend.api import adsb, fantasy, markets, sports, throttle
from backend.api.throttle import Throttle

HTML = "<html><body>502 Bad Gateway</body></html>"
BOARD = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "BTC-USD"]  # config/defaults.yaml


def html_page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})


def fresh_budgets(monkeypatch) -> None:
    for module, name in (
        (markets, "_lookups"), (sports, "_lookups"),
        (fantasy, "_detail_lookups"), (fantasy, "_player_lookups"),
    ):
        budget = getattr(module, name)
        monkeypatch.setattr(module, name, Throttle(int(budget.burst), budget.refill_seconds))


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    for module in (adsb, markets, sports, fantasy):
        monkeypatch.setattr(module, "_cache", {})
    monkeypatch.setattr(fantasy, "_player_cache", {})
    fresh_budgets(monkeypatch)


def error_status(result) -> int | None:
    return result.status_code if isinstance(result, JSONResponse) else None


def board(*symbols: str):
    """A request whose live config has these markets symbols (default: BOARD)."""
    config = {"modules": {"markets": {"symbols": list(symbols or BOARD)}}}
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


async def no_pro_teams(season):  # the pro schedule fetch is not under test
    return {}


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
    assert error_status(await markets.market_detail("AAPL", board())) == 502
    assert "AAPL" not in markets._cache
    sent = len(http.requests)
    assert error_status(await markets.market_detail("AAPL", board())) == 502
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
    detail = await markets.market_detail("AAPL", board())
    assert isinstance(detail, dict)
    assert detail["profile"] is None
    assert detail["metrics"]["high52"] == 250.0
    assert [n["headline"] for n in detail["news"]] == ["Apple ships"]
    assert detail["recommendation"]["label"] == "Buy"
    assert markets._cache["AAPL"][1] is detail


async def test_markets_crypto_garbage_is_an_uncached_502(http, finnhub_key):
    http.handler = html_page
    assert error_status(await markets.market_detail("BTC-USD", board())) == 502
    assert "BTC-USD" not in markets._cache


async def test_markets_crypto_news(http, finnhub_key):
    http.handler = lambda request: httpx.Response(200, json=[
        {"headline": "Bitcoin rallies", "summary": "", "datetime": 2},
        {"headline": "Ether slips", "summary": "", "datetime": 1},
    ])
    detail = await markets.market_detail("BTC-USD", board())
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
    monkeypatch.setattr(fantasy, "pro_teams", no_pro_teams)
    http.handler = lambda request: httpx.Response(200, text=body)
    result = await fantasy.fantasy_detail("123", 2026, 8, 1)
    assert error_status(result) == 502
    assert fantasy._cache == {}


# --- caller-chosen values never choose the upstream path ----------------------
# These are plain GETs, so a page on another site can issue them too.

def ok_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


@pytest.mark.parametrize("sport, league, event", [
    ("../../../v3/x?y=", "nfl", "1"),  # reached https://site.api.espn.com/apis/v3/x?event=1
    ("..", "nfl", "1"),
    (".", "nfl", "1"),
    ("foot/ball", "nfl", "1"),
    ("football\n", "nfl", "1"),
    ("", "nfl", "1"),
    ("f" * 41, "nfl", "1"),
    ("football", "nfl/../mlb", "1"),
    ("football", "nfl?x=", "1"),
    ("football", "-nfl", "1"),
    ("football", "nfl", ""),
    ("football", "nfl", "abc"),
    ("football", "nfl", "1&event=2"),
    ("football", "nfl", "1" * 16),
])
async def test_sports_detail_refuses_values_that_are_not_espn_slugs(http, sport, league, event):
    result = await sports.game_detail(sport, league, event)
    assert error_status(result) == 400
    assert http.requests == [] and sports._cache == {}


@pytest.mark.parametrize("sport, league, path", [
    ("football", "NFL", "/apis/site/v2/sports/football/nfl/summary"),  # as the payload has it
    ("soccer", "eng.1", "/apis/site/v2/sports/soccer/eng.1/summary"),
    ("basketball", "mens-college-basketball",
     "/apis/site/v2/sports/basketball/mens-college-basketball/summary"),
])
async def test_sports_detail_asks_espn_for_real_slugs(http, sport, league, path):
    http.handler = ok_json
    assert isinstance(await sports.game_detail(sport, league, "401872948"), dict)
    (sent,) = http.requests
    assert sent.url.host == "site.api.espn.com" and sent.url.path == path
    assert sent.url.params["event"] == "401872948"


BAD_LEAGUES = [
    ("../../../v2/x", 2026, 8),  # on the host the ESPN cookies go to
    ("12/34", 2026, 8),
    ("12?view=x", 2026, 8),
    ("", 2026, 8),
    ("abc", 2026, 8),
    ("123\n", 2026, 8),
    ("1" * 16, 2026, 8),
    ("123", 1999, 8),
    ("123", 20260, 8),
    ("123", -2026, 8),
    ("123", 2026, 0),
    ("123", 2026, 31),
]


@pytest.mark.parametrize("league_id, season, week", BAD_LEAGUES)
async def test_fantasy_detail_refuses_a_league_that_is_not_a_number(
    http, monkeypatch, league_id, season, week
):
    monkeypatch.setattr(fantasy, "pro_teams", no_pro_teams)
    assert error_status(await fantasy.fantasy_detail(league_id, season, week, 1)) == 400
    assert error_status(await fantasy.fantasy_player(league_id, season, week, 1)) == 400
    assert http.requests == [] and fantasy._cache == {} and fantasy._player_cache == {}


async def test_fantasy_proxies_ask_espn_for_a_real_league(http, monkeypatch):
    monkeypatch.setattr(fantasy, "pro_teams", no_pro_teams)
    http.handler = ok_json
    assert isinstance(await fantasy.fantasy_detail("1234567", 2026, 8, 3), dict)
    # D/ST "players" have negative ids
    assert isinstance(await fantasy.fantasy_player("1234567", 2026, 8, -16001), dict)
    league = "/apis/v3/games/ffl/seasons/2026/segments/0/leagues/1234567"
    assert {r.url.path for r in http.requests} == {league}
    assert {r.url.host for r in http.requests} == {"lm-api-reads.fantasy.espn.com"}


@pytest.mark.parametrize("callsign", ["../../v0/aircraft/x", "DAL/123", "DAL 123", "DAL123456", "?", ""])
async def test_adsb_route_refuses_what_is_not_a_callsign(http, callsign):
    assert await adsb.adsb_route(callsign) == {"route": None}
    assert http.requests == [] and adsb._cache == {}


# --- markets: only the symbols on the board ------------------------------------

@pytest.mark.parametrize("symbol", ["GME", "AAPL/../x", "BTC-USDT", ""])
async def test_markets_detail_is_only_for_configured_symbols(http, finnhub_key, symbol):
    # each new symbol used to cost five Finnhub calls on the real key
    assert error_status(await markets.market_detail(symbol, board())) == 404
    assert http.requests == [] and markets._cache == {}


async def test_markets_detail_serves_every_configured_symbol(http, finnhub_key):
    http.handler = lambda request: httpx.Response(200, json=[])
    request = board("aapl", "BRK.B", "DOGE-USD")  # a hand-edited config may be lower case
    for symbol in ("AAPL", "aapl", "BRK.B", "DOGE-USD", "doge-usd"):
        assert isinstance(await markets.market_detail(symbol, request), dict), symbol
    assert set(markets._cache) == {"AAPL", "BRK.B", "DOGE-USD"}
    asked = {r.url.params.get("symbol") for r in http.requests if "symbol" in r.url.params}
    assert asked == {"AAPL", "BRK.B"}  # crypto news has no symbol parameter


async def test_markets_detail_follows_the_collectors_default_symbols(http, finnhub_key):
    http.handler = lambda request: httpx.Response(200, json=[])
    bare = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config={"modules": {}})))
    assert isinstance(await markets.market_detail("SPY", bare), dict)
    assert error_status(await markets.market_detail("AAPL", bare)) == 404


# --- uncached lookups are budgeted; cached ones are free -----------------------

@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(throttle, "_now", lambda: now["t"])
    fresh_budgets(monkeypatch)  # full, as of this clock
    return now


def test_throttle_allows_a_burst_then_refills(clock):
    budget = Throttle(burst=3, refill_seconds=10.0)
    assert [budget.take() for _ in range(4)] == [True, True, True, False]
    assert budget.retry_after() == 10
    clock["t"] += 4
    assert not budget.take() and budget.retry_after() == 6
    clock["t"] += 6
    assert budget.take() and not budget.take()
    clock["t"] += 3600  # never more than the burst, however long it sat idle
    assert [budget.take() for _ in range(4)] == [True, True, True, False]


def retry_after(result) -> int:
    assert error_status(result) == 429, result
    return int(result.headers["retry-after"])


async def test_markets_detail_budget(http, finnhub_key, clock):
    http.handler = lambda request: httpx.Response(200, json=[])
    symbols = [f"SYM{i}" for i in range(8)]
    request = board(*symbols)
    for symbol in symbols[:6]:  # the whole default board of equities, tapped in a row
        assert isinstance(await markets.market_detail(symbol, request), dict)
    sent = len(http.requests)
    assert sent == 6 * 5
    assert retry_after(await markets.market_detail(symbols[6], request)) == 20
    assert isinstance(await markets.market_detail(symbols[0], request), dict)  # cached: free
    assert len(http.requests) == sent
    clock["t"] += 20
    assert isinstance(await markets.market_detail(symbols[6], request), dict)
    assert error_status(await markets.market_detail(symbols[7], request)) == 429


async def test_markets_detail_without_a_key_spends_nothing(http):
    for _ in range(20):
        assert await markets.market_detail("AAPL", board()) == {
            "profile": None, "metrics": None, "news": [],
        }
    assert http.requests == [] and markets._lookups.take()


async def test_sports_detail_budget(http, clock):
    http.handler = ok_json
    for event in range(10):
        assert isinstance(await sports.game_detail("football", "nfl", str(event)), dict)
    assert retry_after(await sports.game_detail("football", "nfl", "10")) == 6
    assert isinstance(await sports.game_detail("football", "NFL", "0"), dict)  # cached: free
    assert len(http.requests) == 10
    clock["t"] += 6
    assert isinstance(await sports.game_detail("football", "nfl", "10"), dict)


async def test_a_failing_upstream_is_budgeted_too(http, clock):
    # failures aren't cached, so every retry is another request to ESPN
    http.handler = html_page
    for _ in range(10):
        assert error_status(await sports.game_detail("football", "nfl", "401")) == 502
    assert error_status(await sports.game_detail("football", "nfl", "401")) == 429
    assert len(http.requests) == 10


async def test_fantasy_budgets(http, monkeypatch, clock):
    monkeypatch.setattr(fantasy, "pro_teams", no_pro_teams)
    http.handler = ok_json
    for team in range(10):
        assert isinstance(await fantasy.fantasy_detail("123", 2026, 8, team), dict)
    assert retry_after(await fantasy.fantasy_detail("123", 2026, 8, 10)) == 6
    assert isinstance(await fantasy.fantasy_detail("123", 2026, 8, 0), dict)  # cached: free
    assert len(http.requests) == 10

    for player in range(6):  # its own budget: a card is up to ten requests
        assert isinstance(await fantasy.fantasy_player("123", 2026, 8, player), dict)
    sent = len(http.requests)
    assert retry_after(await fantasy.fantasy_player("123", 2026, 8, 6)) == 10
    assert isinstance(await fantasy.fantasy_player("123", 2026, 8, 0), dict)  # cached: free
    assert len(http.requests) == sent


# --- over HTTP, as a browser sends them ------------------------------------------

@pytest.mark.parametrize("url, status", [
    ("/api/sports/detail?sport=..%2F..%2F..%2Fv3%2Fx%3Fy%3D&league=nfl&event=1", 400),
    ("/api/sports/detail?sport=football&league=nfl", 422),  # FastAPI: event missing
    ("/api/fantasy/detail?league_id=..%2F..&season=2026&week=8&team_id=1", 400),
    ("/api/fantasy/detail?league_id=123&season=twenty&week=8&team_id=1", 422),
    ("/api/fantasy/player?league_id=123&season=2026&week=99&player_id=1", 400),
    ("/api/markets/detail?symbol=GME", 404),
    ("/api/adsb/route?callsign=..%2F..", 200),  # {"route": null}, as for no callsign
])
async def test_refusals_over_http(url, status):
    app = FastAPI()
    for module in (adsb, fantasy, markets, sports):
        app.include_router(module.router, prefix="/api")
    app.state.config = {"modules": {"markets": {"symbols": BOARD}}}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ticker") as client:
        response = await client.get(url)
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json() == {"route": None}
