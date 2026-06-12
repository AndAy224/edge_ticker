"""On-demand game detail for the display's tap-to-expand view.

Proxies ESPN's per-game summary endpoint and curates the interesting bits
(win probability, odds, recent form, head-to-head, venue). Fetched only when
a game is tapped — never polled — with a small TTL cache so repeated taps
don't hammer ESPN.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..collectors.sports import SUMMARY_URL

router = APIRouter()

CACHE_TTL_SECONDS = 120.0
CACHE_MAX_ENTRIES = 50
_cache: dict[str, tuple[float, dict]] = {}


def _probability(summary: dict) -> dict | None:
    predictor = summary.get("predictor") or {}
    home = (predictor.get("homeTeam") or {}).get("gameProjection")
    away = (predictor.get("awayTeam") or {}).get("gameProjection")
    if home is not None and away is not None:
        try:
            return {"home_pct": float(home), "away_pct": float(away)}
        except (TypeError, ValueError):
            pass
    points = summary.get("winprobability") or []
    if points:
        try:
            home_pct = float(points[-1].get("homeWinPercentage")) * 100
            return {"home_pct": round(home_pct, 1), "away_pct": round(100 - home_pct, 1)}
        except (TypeError, ValueError):
            pass
    return None


def _odds(summary: dict) -> dict | None:
    for key in ("pickcenter", "odds"):
        entries = summary.get(key) or []
        if entries:
            entry = entries[0]
            details = entry.get("details")
            over_under = entry.get("overUnder")
            if details or over_under:
                return {"details": details, "over_under": over_under}
    return None


def _last_meeting(summary: dict) -> dict | None:
    for series in summary.get("seasonseries") or []:
        completed = [e for e in series.get("events", []) if e.get("statusType", {}).get("completed")]
        if not completed:
            continue
        event = completed[-1]
        parts = []
        for c in event.get("competitors", []):
            parts.append(f"{(c.get('team') or {}).get('abbreviation', '?')} {c.get('score', '')}")
        return {"text": " — ".join(parts), "date": event.get("date")}
    return None


def _last_games(summary: dict) -> dict:
    sides: dict[str, list] = {}
    for block in summary.get("lastFiveGames") or []:
        team = block.get("team") or {}
        side = "home" if block.get("displayOrder") == 1 else "away"
        games = []
        for event in (block.get("events") or [])[:3]:
            games.append(
                {
                    "result": event.get("gameResult"),
                    "score": event.get("score"),
                    "opponent": (event.get("opponent") or {}).get("abbreviation"),
                    "at_vs": event.get("atVs"),
                    "date": event.get("gameDate"),
                }
            )
        sides[side] = games
        sides.setdefault("abbrevs", []).append(team.get("abbreviation"))  # type: ignore[arg-type]
    return sides


def _last_play(summary: dict) -> str | None:
    plays = summary.get("scoringPlays") or summary.get("plays") or []
    if plays:
        return plays[-1].get("text")
    return None


@router.get("/sports/detail")
async def game_detail(sport: str, league: str, event: str):
    key = f"{sport}/{league}/{event}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                SUMMARY_URL.format(sport=sport, league=league.lower()),
                params={"event": event},
            )
            response.raise_for_status()
            summary = response.json()
    except Exception as exc:
        return JSONResponse({"error": f"summary fetch failed: {exc}"}, status_code=502)

    game_info = summary.get("gameInfo") or {}
    broadcasts = summary.get("broadcasts") or []
    detail = {
        "venue": (game_info.get("venue") or {}).get("fullName"),
        "broadcast": ((broadcasts[0].get("media") or {}).get("shortName") if broadcasts else None),
        "probability": _probability(summary),
        "odds": _odds(summary),
        "last_meeting": _last_meeting(summary),
        "last_games": _last_games(summary),
        "last_play": _last_play(summary),
    }
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[key] = (now, detail)
    return detail
