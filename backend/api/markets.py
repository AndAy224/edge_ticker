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


async def _equity_detail(client: httpx.AsyncClient, symbol: str, key: str) -> dict:
    today = date.today()
    profile_r, metric_r, news_r = await asyncio.gather(
        client.get(f"{FINNHUB}/stock/profile2", params={"symbol": symbol, "token": key}),
        client.get(f"{FINNHUB}/stock/metric", params={"symbol": symbol, "metric": "all", "token": key}),
        client.get(
            f"{FINNHUB}/company-news",
            params={
                "symbol": symbol,
                "from": str(today - timedelta(days=7)),
                "to": str(today),
                "token": key,
            },
        ),
        return_exceptions=True,
    )
    profile = None
    if not isinstance(profile_r, BaseException) and profile_r.status_code == 200:
        p = profile_r.json()
        if p.get("name"):
            profile = {
                "name": p.get("name"),
                "industry": p.get("finnhubIndustry"),
                "exchange": (p.get("exchange") or "").split(" - ")[0],
                "market_cap": p.get("marketCapitalization"),  # $ millions
            }
    metrics = None
    if not isinstance(metric_r, BaseException) and metric_r.status_code == 200:
        m = metric_r.json().get("metric") or {}
        metrics = {
            "high52": m.get("52WeekHigh"),
            "low52": m.get("52WeekLow"),
            "pe": m.get("peTTM"),
            "beta": m.get("beta"),
            "div_yield": m.get("currentDividendYieldTTM"),
        }
    news: list[dict] = []
    if not isinstance(news_r, BaseException) and news_r.status_code == 200:
        body = news_r.json()
        if isinstance(body, list):
            news = _shape_news(body)
    return {"profile": profile, "metrics": metrics, "news": news}


async def _crypto_detail(client: httpx.AsyncClient, symbol: str, key: str) -> dict:
    base = symbol[:-4]  # strip -USD
    name = CRYPTO_NAMES.get(base, base.lower())
    detail: dict = {"profile": {"name": name.title()}, "metrics": None, "news": []}
    try:
        response = await client.get(
            f"{FINNHUB}/news", params={"category": "crypto", "token": key}
        )
        if response.status_code == 200 and isinstance(response.json(), list):
            matched = [
                item
                for item in response.json()
                if name in (item.get("headline", "") + item.get("summary", "")).lower()
                or base.lower() in (item.get("headline") or "").lower()
            ]
            detail["news"] = _shape_news(matched)
    except httpx.HTTPError:
        pass
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
    async with httpx.AsyncClient(timeout=15) as client:
        if symbol.endswith("-USD"):
            detail = await _crypto_detail(client, symbol, key)
        else:
            detail = await _equity_detail(client, symbol, key)
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[symbol] = (now, detail)
    return detail
