"""Markets collector.

Polling baseline, picked automatically:
- FINNHUB_KEY set  → Finnhub REST /quote per symbol (free tier has no candles,
  so sparklines are self-built from accumulated quote history, persisted to
  data/markets-spark.json across restarts)
- otherwise        → Yahoo Finance chart API (keyless, includes sparkline)

With FINNHUB_KEY, a Finnhub WebSocket stream also runs alongside the poll
loop and publishes real-time price updates between polls (throttled to one
publish per ~2s). Crypto symbols like BTC-USD map to Binance pairs on the
stream.

Yahoo aggressively rate-limits non-browser clients, so we keep one persistent
client (cookies survive between polls) and warm it up against finance.yahoo.com
when a 401/429 appears. Persistent failures degrade to a stale module via the
base-class backoff — never a crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
import websockets

from ..db import DB_PATH
from ..state import Bus, ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_WARMUP_URL = "https://finance.yahoo.com"
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
FINNHUB_WS_URL = "wss://ws.finnhub.io"
STREAM_PUBLISH_INTERVAL = 2.0

# Self-built sparkline history: Finnhub's free tier has no candles and Yahoo
# (which includes intraday closes) rate-limits some networks, so we accumulate
# our own series from the quotes we already receive. Persisted to JSON so a
# backend restart doesn't blank the sparklines.
HISTORY_WINDOW_SECONDS = 8 * 3600
HISTORY_STREAM_INTERVAL = 60.0  # min spacing for stream-sourced points
SPARK_POINTS = 48


class MarketsCollector(Collector):
    name = "markets"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.symbols: list[str] = self.module_config.get("symbols", ["SPY", "QQQ"])
        self.interval = float(self.module_config.get("poll_seconds", 60))
        self.finnhub_key = os.environ.get("FINNHUB_KEY", "").strip()
        self._client: httpx.AsyncClient | None = None
        self._warmed_up = False
        self._quotes: dict[str, dict] = {}  # latest shaped quote per symbol
        # stream symbol ↔ config symbol (BTC-USD ↔ BINANCE:BTCUSDT)
        self._stream_map = {self._stream_symbol(s): s for s in self.symbols}
        self._spark_file = DB_PATH.parent / "markets-spark.json"
        self._history: dict[str, list[list[float]]] = self._load_history()
        self._history_at: dict[str, float] = {}  # last stream-sourced point per symbol

    @staticmethod
    def _stream_symbol(symbol: str) -> str:
        if symbol.endswith("-USD"):
            return f"BINANCE:{symbol[:-4]}USDT"
        return symbol

    # ---- sparkline history -------------------------------------------------

    def _load_history(self) -> dict[str, list[list[float]]]:
        try:
            raw = json.loads(self._spark_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        cutoff = time.time() - HISTORY_WINDOW_SECONDS
        return {
            symbol: [p for p in points if p[0] >= cutoff]
            for symbol, points in raw.items()
            if symbol in self.symbols
        }

    def _save_history(self) -> None:
        try:
            tmp = self._spark_file.with_suffix(".json.tmp")
            self._spark_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._history), encoding="utf-8")
            os.replace(tmp, self._spark_file)
        except OSError as exc:
            log.debug("could not persist spark history: %s", exc)

    def _record(self, symbol: str, price: float, now: float) -> None:
        points = self._history.setdefault(symbol, [])
        points.append([now, price])
        cutoff = now - HISTORY_WINDOW_SECONDS
        if points and points[0][0] < cutoff:
            self._history[symbol] = [p for p in points if p[0] >= cutoff]

    def _spark(self, symbol: str) -> list[float]:
        prices = [p[1] for p in self._history.get(symbol, [])]
        if len(prices) <= SPARK_POINTS:
            return prices
        step = (len(prices) - 1) / (SPARK_POINTS - 1)
        return [prices[round(i * step)] for i in range(SPARK_POINTS)]

    async def start(self, bus: Bus) -> None:
        if not self.finnhub_key:
            await super().start(bus)
            return
        stream = asyncio.create_task(self._stream(bus), name="markets-stream")
        try:
            await super().start(bus)
        finally:
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)

    async def _stream(self, bus: Bus) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    f"{FINNHUB_WS_URL}?token={self.finnhub_key}"
                ) as ws:
                    for stream_symbol in self._stream_map:
                        await ws.send(
                            json.dumps({"type": "subscribe", "symbol": stream_symbol})
                        )
                    backoff = 1.0
                    pending: dict[str, float] = {}
                    last_publish = 0.0
                    async for raw in ws:
                        message = json.loads(raw)
                        if message.get("type") != "trade":
                            continue
                        for trade in message.get("data", []):
                            symbol = self._stream_map.get(trade.get("s"))
                            if symbol and trade.get("p"):
                                pending[symbol] = trade["p"]
                        now = asyncio.get_running_loop().time()
                        if pending and now - last_publish >= STREAM_PUBLISH_INTERVAL:
                            payload = self._apply_live(pending)
                            pending.clear()
                            last_publish = now
                            if payload is not None:
                                self._last_payload = payload
                                await bus.publish(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("markets stream error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _apply_live(self, prices: dict[str, float]) -> ModulePayload | None:
        """Fold streamed trade prices into the last polled quotes and reshape."""
        if not self._quotes:
            return None
        changed = False
        now = time.time()
        for symbol, price in prices.items():
            quote = self._quotes.get(symbol)
            if quote is None or quote["price"] == price:
                continue
            previous = quote["price"] - quote["change"]  # poll-time prev close
            quote["price"] = price
            quote["change"] = price - previous
            quote["pct"] = (quote["change"] / previous * 100) if previous else 0.0
            if now - self._history_at.get(symbol, 0.0) >= HISTORY_STREAM_INTERVAL:
                self._history_at[symbol] = now
                self._record(symbol, price, now)
                quote["spark"] = self._spark(symbol)
            changed = True
        if not changed:
            return None
        return self.shape([self._quotes[s] for s in self.symbols if s in self._quotes])

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            self._warmed_up = False
        return self._client

    async def fetch(self) -> list[dict]:
        client = self._get_client()
        quote = self._finnhub_quote if self.finnhub_key else self._yahoo_quote
        results = await asyncio.gather(
            *(quote(client, s) for s in self.symbols), return_exceptions=True
        )
        quotes = []
        rate_limited = False
        for symbol, result in zip(self.symbols, results):
            if isinstance(result, BaseException):
                log.debug("quote failed for %s: %s", symbol, result)
                if (
                    isinstance(result, httpx.HTTPStatusError)
                    and result.response.status_code in (401, 429)
                ):
                    rate_limited = True
            else:
                quotes.append(result)
        if not quotes:
            if rate_limited:
                self._warmed_up = False  # force a fresh warm-up next round
            raise RuntimeError("all quote requests failed")
        now = time.time()
        for q in quotes:
            self._record(q["symbol"], q["price"], now)
            if not q["spark"]:  # Finnhub path — fill from accumulated history
                q["spark"] = self._spark(q["symbol"])
        self._save_history()
        return quotes

    async def _yahoo_quote(self, client: httpx.AsyncClient, symbol: str) -> dict:
        if not self._warmed_up:
            self._warmed_up = True
            try:
                await client.get(YAHOO_WARMUP_URL)
            except httpx.HTTPError:
                pass  # warm-up is best-effort
        response = await client.get(
            YAHOO_CHART_URL.format(symbol=symbol),
            params={"range": "1d", "interval": "5m", "includePrePost": "false"},
        )
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result["meta"]
        price = meta["regularMarketPrice"]
        previous = meta.get("chartPreviousClose") or meta.get("previousClose") or price
        quote_bars = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = [c for c in quote_bars.get("close") or [] if c is not None]
        change = price - previous
        return {
            "symbol": symbol,
            "price": price,
            "change": change,
            "pct": (change / previous * 100) if previous else 0.0,
            "spark": closes[-48:],
            "currency": meta.get("currency"),
            "market_state": meta.get("marketState"),
        }

    async def _finnhub_quote(self, client: httpx.AsyncClient, symbol: str) -> dict:
        response = await client.get(
            FINNHUB_QUOTE_URL, params={"symbol": symbol, "token": self.finnhub_key}
        )
        response.raise_for_status()
        data = response.json()
        price = data.get("c")
        if not price:  # Finnhub returns zeros for unknown symbols
            raise RuntimeError(f"no Finnhub quote for {symbol}")
        return {
            "symbol": symbol,
            "price": price,
            "change": data.get("d") or 0.0,
            "pct": data.get("dp") or 0.0,
            "spark": [],  # candles are not on the free REST tier
            "currency": "USD",
            "market_state": None,
        }

    def shape(self, quotes: list[dict]) -> ModulePayload:
        self._quotes = {q["symbol"]: q for q in quotes}
        tape = []
        for q in quotes:
            up = q["change"] >= 0
            tape.append(
                TapeItem(
                    text=f"{q['symbol']} {q['price']:,.2f} "
                    f"{'▲' if up else '▼'} {abs(q['pct']):.2f}%",
                    accent="up" if up else "down",
                )
            )
        return ModulePayload(module=self.name, stage={"quotes": quotes}, tape=tape)
