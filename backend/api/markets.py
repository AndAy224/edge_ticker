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
    profile_r, metric_r, news_r, earnings_r, rec_r = await asyncio.gather(
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
        client.get(
            f"{FINNHUB}/calendar/earnings",
            params={
                "symbol": symbol,
                "from": str(today),
                "to": str(today + timedelta(days=90)),
                "token": key,
            },
        ),
        client.get(
            f"{FINNHUB}/stock/recommendation", params={"symbol": symbol, "token": key}
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
    earnings = None
    if not isinstance(earnings_r, BaseException) and earnings_r.status_code == 200:
        calendar = earnings_r.json().get("earningsCalendar") or []
        if calendar:
            entry = sorted(calendar, key=lambda e: e.get("date") or "")[0]
            earnings = {
                "date": entry.get("date"),
                "hour": entry.get("hour"),
                "eps_estimate": entry.get("epsEstimate"),
            }
    recommendation = None
    if not isinstance(rec_r, BaseException) and rec_r.status_code == 200:
        body = rec_r.json()
        if isinstance(body, list) and body:
            latest = body[0]  # newest month first
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
