"""backend/collectors/stocks.py: health, earnings refresh, the Finnhub stream's
reconnect pacing and the client's lifecycle. Finnhub must never be contacted
from dev (one WebSocket per key, and prod holds it), so every upstream here is
hand-written."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.collectors import stocks
from backend.collectors.stocks import MarketsCollector
from helpers import FakeBus, json_response, until

EASTERN = timezone(timedelta(hours=-4))  # the appliance's local zone (EDT)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 10, 27, 21, 0, tzinfo=EASTERN)}
    monkeypatch.setattr(stocks, "_now", lambda: now["t"])
    return now


@pytest.fixture
def collector(defaults_config, monkeypatch, tmp_path) -> MarketsCollector:
    monkeypatch.setenv("FINNHUB_KEY", "test-key")  # only ever sent to mocks
    defaults_config["modules"]["markets"]["symbols"] = ["SPY", "QQQ", "BTC-USD"]
    collector = MarketsCollector(defaults_config)
    collector._spark_file = tmp_path / "spark.json"
    return collector


FINNHUB_SPY = {"c": 500.0, "d": 5.0, "dp": 1.0, "o": 496.0, "h": 501.0, "l": 495.0, "pc": 495.0}
COINBASE_STATS = {"last": "100000", "open": "99000", "high": "101000", "low": "98000"}


class Upstream:
    """Finnhub + Coinbase answers, switchable per test step."""

    def __init__(self) -> None:
        self.quotes = {"SPY": FINNHUB_SPY, "QQQ": FINNHUB_SPY}
        self.coinbase_status = 200
        self.earnings: dict[str, list[str]] = {}  # date -> reporting symbols
        self.earnings_status = 200
        self.earnings_dates: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.exchange.coinbase.com":
            if self.coinbase_status != 200:
                return httpx.Response(self.coinbase_status)
            return json_response(COINBASE_STATS if path.endswith("/stats") else [])
        if path.endswith("/quote"):
            return json_response(self.quotes.get(request.url.params["symbol"], {"c": 0}))
        if path.endswith("/calendar/earnings"):
            day = request.url.params["from"]
            self.earnings_dates.append(day)
            if self.earnings_status != 200:
                return httpx.Response(self.earnings_status)
            symbols = self.earnings.get(day, [])
            return json_response({"earningsCalendar": [{"symbol": s} for s in symbols]})
        return httpx.Response(404)


async def test_missing_quotes_are_degraded(collector, http, clock):
    upstream = Upstream()
    upstream.quotes = {"SPY": FINNHUB_SPY}  # QQQ: Finnhub answers zeros
    upstream.coinbase_status = 503
    http.handler = upstream
    quotes = await collector.fetch()
    assert [q["symbol"] for q in quotes] == ["SPY"]
    assert collector.degraded == "no quote for QQQ, BTC-USD"

    upstream.quotes = {"SPY": FINNHUB_SPY, "QQQ": FINNHUB_SPY}
    upstream.coinbase_status = 200
    assert len(await collector.fetch()) == 3
    assert collector.degraded is None
    await collector._client.aclose()


async def test_earnings_follow_the_local_date(collector, http, clock):
    upstream = Upstream()
    upstream.earnings = {"2026-10-27": ["QQQ"], "2026-10-28": ["SPY"], "2026-10-29": ["BTC-USD"]}
    http.handler = upstream

    def tape() -> list[str]:
        return [t.text for t in collector.shape([]).tape]  # earnings alerts only, without quotes

    await collector.fetch()  # 21:00
    assert tape() == ["QQQ reports earnings today"]
    clock["t"] += timedelta(hours=2)  # 23:00, same day: no new request
    await collector.fetch()
    assert upstream.earnings_dates == ["2026-10-27"]

    # Just past midnight: today's reporters, not yesterday's until 09:00.
    clock["t"] = datetime(2026, 10, 28, 0, 1, tzinfo=EASTERN)
    await collector.fetch()
    assert upstream.earnings_dates[-1] == "2026-10-28"
    assert tape() == ["SPY reports earnings today"]

    # A failed fetch on a new day clears yesterday's set, is reported, and is
    # retried within minutes rather than 12 hours.
    upstream.earnings_status = 502
    clock["t"] = datetime(2026, 10, 29, 0, 1, tzinfo=EASTERN)
    await collector.fetch()
    assert tape() == []
    assert collector.degraded == "earnings calendar: HTTP 502"
    clock["t"] += timedelta(minutes=5)
    await collector.fetch()
    assert len(upstream.earnings_dates) == 3  # inside the retry delay: no request
    upstream.earnings_status = 200
    clock["t"] += stocks.EARNINGS_RETRY
    await collector.fetch()
    assert tape() == ["BTC-USD reports earnings today"]
    assert collector.degraded is None
    assert upstream.earnings_dates == ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-29"]
    await collector._client.aclose()


class FakeSocket:
    def __init__(self, frames: list, hang: bool = False):
        self.frames, self.sent, self.hang = list(frames), [], hang
        self.close_code, self.close_reason = 1000, ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        for frame in self.frames:
            yield frame
            await asyncio.sleep(0)
        if self.hang:
            await asyncio.Event().wait()


@pytest.fixture
def fake_stream(monkeypatch):
    calls: list[str] = []
    sockets: list[FakeSocket] = []
    delays: list[float] = []

    def connect(url, **kwargs):
        calls.append(url)
        return sockets.pop(0) if sockets else FakeSocket([], hang=True)

    async def no_sleep(seconds):
        delays.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(stocks.websockets, "connect", connect)
    monkeypatch.setattr(stocks, "_sleep", no_sleep)
    return calls, sockets, delays


async def test_stream_backs_off_after_every_close(collector, fake_stream):
    """websockets ends `async for` quietly on a normal close; Finnhub closing
    an accepted socket (dev and prod sharing a key) used to mean an immediate
    reconnect, forever. The backoff resets only once a trade arrives."""
    calls, sockets, delays = fake_stream
    trade = json.dumps({"type": "trade", "data": [{"s": "SPY", "p": 501.0}]})
    ping = json.dumps({"type": "ping"})
    first = FakeSocket([ping])
    sockets.extend([first, FakeSocket([]), FakeSocket([trade]), FakeSocket([])])
    task = asyncio.create_task(collector._stream(FakeBus()))
    try:
        await until(lambda: len(calls) == 5)  # four closes, then a socket that stays open
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [m["symbol"] for m in first.sent] == ["SPY", "QQQ", "BINANCE:BTCUSDT"]
    base = stocks.STREAM_BACKOFF[0]
    expected = [base, 2 * base, base, 2 * base]  # the trade on the third socket reset it
    assert len(delays) == 4
    for delay, floor in zip(delays, expected):
        assert floor <= delay <= floor * 1.25


@pytest.mark.parametrize("with_key", [False, True], ids=["yahoo", "finnhub"])
async def test_restart_closes_the_http_client(defaults_config, http, fake_stream, monkeypatch, tmp_path, with_key):
    """A config save cancels the collector task and builds a new instance;
    the old instance's persistent client used to stay open."""
    if with_key:
        monkeypatch.setenv("FINNHUB_KEY", "test-key")
    collector = MarketsCollector(defaults_config)
    collector._spark_file = tmp_path / "spark.json"
    http.handler = lambda request: httpx.Response(503)
    task = asyncio.create_task(collector.start(FakeBus()))
    stream_calls = fake_stream[0]
    try:
        # Polled once (now backing off) and, with a key, the stream is open.
        await until(lambda: collector.failures_total > 0 and len(stream_calls) == int(with_key))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert collector._client is not None and collector._client.is_closed
