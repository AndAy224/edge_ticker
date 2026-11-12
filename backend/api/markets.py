"""On-demand stock/crypto detail for the display's tap-to-expand view.

Curates Finnhub free-tier data (company profile, key metrics, recent
headlines) for a tapped symbol. Fetched only on tap, never polled; small TTL
cache so repeated taps don't burn API quota.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, timedelta

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

FINNHUB = "https://finnhub.io/api/v1"
CACHE_TTL_SECONDS = 300.0
CACHE_MAX_ENTRIES = 50
NEWS_LIMIT = 5
_cache: dict[str, tuple[float, dict]] = {}

CRYPTO_NAMES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "DOGE": "dogecoin",
    "SOL": "solana",
    "XRP": "xrp",
    "ADA": "cardano",
    "LTC": "litecoin",
}


def _shape_news(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    news = []
    for item in sorted(items, key=lambda i: i.get("datetime") or 0, reverse=True):
        headline = (item.get("headline") or "").strip()
        if not headline or headline in seen:
            continue
        seen.add(headline)
        news.append(
            {
                "headline": headline,
                "source": item.get("source"),
                "summary": (item.get("summary") or "")[:200],
                "datetime": item.get("datetime"),
            }
        )
        if len(news) >= NEWS_LIMIT:
            break
    return news


def _body(result: httpx.Response | BaseException):
    """The decoded JSON of a 200, else None. A 200 whose body isn't JSON (an
    HTML error page from a proxy in front of Finnhub) used to raise out of the
    handler as an uncached 500; it now counts as that section failing."""
    if isinstance(result, BaseException) or result.status_code != 200:
        return None
    try:
        return result.json()
    except ValueError:
        return None


async def _equity_detail(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """None when no section got an answer, so nothing worth caching came back."""
    today = date.today()
    results = await asyncio.gather(
        client.get(f"{FINNHUB}/stock/profile2", params={"symbol": symbol}),
        client.get(f"{FINNHUB}/stock/metric", params={"symbol": symbol, "metric": "all"}),
        client.get(
            f"{FINNHUB}/company-news",
            params={
                "symbol": symbol,
                "from": str(today - timedelta(days=7)),
                "to": str(today),
            },
        ),
        client.get(
            f"{FINNHUB}/calendar/earnings",
            params={
                "symbol": symbol,
                "from": str(today),
                "to": str(today + timedelta(days=90)),
            },
        ),
        client.get(
            f"{FINNHUB}/stock/recommendation", params={"symbol": symbol}
        ),
        return_exceptions=True,
    )
    bodies = [_body(r) for r in results]
    if all(b is None for b in bodies):
        return None
    p, metric_b, news_b, earnings_b, rec_b = bodies
    profile = None
    if isinstance(p, dict) and p.get("name"):
        profile = {
            "name": p.get("name"),
            "industry": p.get("finnhubIndustry"),
            "exchange": (p.get("exchange") or "").split(" - ")[0],
            "market_cap": p.get("marketCapitalization"),  # $ millions
        }
    metrics = None
    if isinstance(metric_b, dict):
        m = metric_b.get("metric")
        m = m if isinstance(m, dict) else {}
        metrics = {
            "high52": m.get("52WeekHigh"),
            "low52": m.get("52WeekLow"),
            "pe": m.get("peTTM"),
            "beta": m.get("beta"),
            "div_yield": m.get("currentDividendYieldTTM"),
        }
    news: list[dict] = []
    if isinstance(news_b, list):
        news = _shape_news(news_b)
    earnings = None
    if isinstance(earnings_b, dict):
        calendar = earnings_b.get("earningsCalendar") or []
        if isinstance(calendar, list) and calendar:
            entry = sorted(calendar, key=lambda e: e.get("date") or "")[0]
            earnings = {
                "date": entry.get("date"),
                "hour": entry.get("hour"),
                "eps_estimate": entry.get("epsEstimate"),
            }
    recommendation = None
    if isinstance(rec_b, list) and rec_b and isinstance(rec_b[0], dict):
        latest = rec_b[0]  # newest month first
        counts = {
            "strong_buy": latest.get("strongBuy") or 0,
            "buy": latest.get("buy") or 0,
            "hold": latest.get("hold") or 0,
            "sell": latest.get("sell") or 0,
            "strong_sell": latest.get("strongSell") or 0,
        }
        total = sum(counts.values())
        if total:
            score = (
                5 * counts["strong_buy"]
                + 4 * counts["buy"]
                + 3 * counts["hold"]
                + 2 * counts["sell"]
                + 1 * counts["strong_sell"]
            ) / total
            label = (
                "Strong Buy"
                if score >= 4.5
                else "Buy"
                if score >= 3.5
                else "Hold"
                if score >= 2.5
                else "Sell"
                if score >= 1.5
                else "Strong Sell"
            )
            recommendation = {**counts, "total": total, "label": label}
    return {
        "profile": profile,
        "metrics": metrics,
        "news": news,
        "earnings": earnings,
        "recommendation": recommendation,
    }


async def _crypto_detail(client: httpx.AsyncClient, symbol: str) -> dict | None:
    base = symbol[:-4]  # strip -USD
    name = CRYPTO_NAMES.get(base, base.lower())
    detail: dict = {"profile": {"name": name.title()}, "metrics": None, "news": []}
    try:
        response = await client.get(
            f"{FINNHUB}/news", params={"category": "crypto"}
        )
    except httpx.HTTPError:
        return None
    body = _body(response)
    if body is None:
        return None
    if isinstance(body, list):
        matched = [
            item
            for item in body
            if name in (item.get("headline", "") + item.get("summary", "")).lower()
            or base.lower() in (item.get("headline") or "").lower()
        ]
        detail["news"] = _shape_news(matched)
    return detail


@router.get("/markets/detail")
async def market_detail(symbol: str):
    symbol = symbol.upper()
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    key = os.environ.get("FINNHUB_KEY", "").strip()
    if not key:
        return {"profile": None, "metrics": None, "news": []}
    # Key in a header, not a `token` query param: request URLs end up in logs.
    async with httpx.AsyncClient(timeout=15, headers={"X-Finnhub-Token": key}) as client:
        if symbol.endswith("-USD"):
            detail = await _crypto_detail(client, symbol)
        else:
            detail = await _equity_detail(client, symbol)
    if detail is None:
        # Finnhub unreachable or answering garbage: an error the display skips,
        # not an empty card cached for five minutes.
        return JSONResponse({"error": "finnhub fetch failed"}, status_code=502)
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[symbol] = (now, detail)
    return detail
