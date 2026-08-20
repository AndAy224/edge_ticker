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
    """The book's full line. Every field here is already in pickcenter[0]."""
    for key in ("pickcenter", "odds"):
        entries = summary.get(key) or []
        if not entries:
            continue
        entry = entries[0]
        home, away = entry.get("homeTeamOdds") or {}, entry.get("awayTeamOdds") or {}
        favorite = "home" if home.get("favorite") else "away" if away.get("favorite") else None
        odds = {
            "details": entry.get("details"),
            "over_under": entry.get("overUnder"),
            "spread": entry.get("spread"),
            "favorite_side": favorite,
            "moneyline": {"home": home.get("moneyLine"), "away": away.get("moneyLine")},
            "provider": (entry.get("provider") or {}).get("name"),
        }
        if any((odds["details"], odds["over_under"], odds["spread"])):
            return odds
    return None


def _prob_curve(summary: dict, points: int = 40) -> list[float] | None:
    """Home win% over the course of the game, downsampled for a sparkline.

    The full array is one entry per play (75+ for a ball game); we already pay
    to download and parse it for the single last value, so the shape is free.
    """
    raw = summary.get("winprobability") or []
    if len(raw) < 4:
        return None
    step = max(1, len(raw) // points)
    sampled = [raw[i] for i in range(0, len(raw), step)]
    if sampled[-1] is not raw[-1]:
        sampled.append(raw[-1])
    out = []
    for entry in sampled:
        value = entry.get("homeWinPercentage")
        if value is None:
            continue
        try:
            out.append(round(float(value) * 100, 1))
        except (TypeError, ValueError):
            continue
    return out or None


def _ats(summary: dict) -> list[dict]:
    """Against-the-spread records, one row per team."""
    rows = []
    for block in summary.get("againstTheSpread") or []:
        team = block.get("team") or {}
        records = block.get("records") or []
        summary_text = None
        for record in records:
            if record.get("summary"):
                summary_text = record["summary"]
                break
        if summary_text:
            rows.append({"abbrev": team.get("abbreviation"), "record": summary_text})
    return rows


def _headline(summary: dict) -> str | None:
    """The AP recap headline for a finished game.

    Deliberately no fallback to summary["news"] — that is general league news
    ("Nick Jonas finds the perfect song"), and showing it under a scoreline
    reads as though it were about this game.
    """
    return (summary.get("article") or {}).get("headline") or None


def _game_extras(summary: dict) -> dict:
    """Weather and attendance — already inside the gameInfo we parse for venue."""
    info = summary.get("gameInfo") or {}
    weather = info.get("weather") or {}
    out: dict = {"attendance": info.get("attendance")}
    if weather:
        temp = weather.get("temperature") or weather.get("highTemperature")
        bits = []
        if temp is not None:
            bits.append(f"{temp}°")
        if weather.get("displayValue"):
            bits.append(weather["displayValue"])
        if weather.get("precipitation") is not None:
            bits.append(f"{weather['precipitation']}% precip")
        out["weather"] = " · ".join(bits) or None
    return out


def _leaders(summary: dict) -> list[dict]:
    """Per-team statistical leaders (football) — headline stat line only."""
    out = []
    for block in summary.get("leaders") or []:
        team = block.get("team") or {}
        entries = []
        for category in (block.get("leaders") or [])[:3]:
            leader = (category.get("leaders") or [{}])[0]
            athlete = leader.get("athlete") or {}
            name = athlete.get("shortName") or athlete.get("displayName")
            if not name:
                continue
            entries.append(
                {
                    "label": (
                        category.get("shortDisplayName")
                        or category.get("abbreviation")
                        or category.get("displayName")
                    ),
                    "name": name,
                    "stat": leader.get("displayValue"),
                }
            )
        if entries:
            out.append({"abbrev": team.get("abbreviation"), "entries": entries})
    return out


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


def _standings(summary: dict) -> list[dict]:
    groups_out = []
    for group in (summary.get("standings") or {}).get("groups") or []:
        rows = []
        for entry in ((group.get("standings") or {}).get("entries") or [])[:6]:
            # ESPN lowercases `type` ("gamesbehind", "winpercent") while `name`
            # is camelCase — key off type and match it in lower case.
            stats = {
                str(s.get("type") or s.get("name") or "").lower(): s.get("displayValue")
                for s in entry.get("stats", [])
            }
            rows.append(
                {
                    "team": entry.get("team"),
                    "wins": stats.get("wins"),
                    "losses": stats.get("losses"),
                    "ties": stats.get("ties"),
                    "games_behind": stats.get("gamesbehind"),
                    "streak": stats.get("streak"),
                    "pct": stats.get("winpercent"),
                }
            )
        if rows:
            groups_out.append({"header": group.get("header"), "rows": rows})
    return groups_out[:2]


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
        "prob_curve": _prob_curve(summary),
        "odds": _odds(summary),
        "ats": _ats(summary),
        "headline": _headline(summary),
        "leaders": _leaders(summary),
        "last_meeting": _last_meeting(summary),
        "last_games": _last_games(summary),
        "last_play": _last_play(summary),
        "standings": _standings(summary),
        **_game_extras(summary),
    }
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[key] = (now, detail)
    return detail
