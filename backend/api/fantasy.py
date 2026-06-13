"""On-demand fantasy boxscore for the display's tap-to-expand view.

The collector's payload carries only the starting lineups; tapping a matchup
fetches the full boxscore (starters + bench, projected vs actual per player)
here, with a small TTL cache so repeated taps don't hammer ESPN. Never polled.
"""
from __future__ import annotations

import os
import re
import time

import httpx
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from ..collectors.fantasy import (
    HEADERS,
    LEAGUE_URL,
    LOGO_IMAGE_PREFIX,
    SLOT_LABELS,
    _logo,
    _player_points,
    _team_name,
)

router = APIRouter()

CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 50
_cache: dict[str, tuple[float, dict]] = {}

# Logo proxy cache: image id -> (monotonic_time, content_type, bytes). Logos
# rarely change, so a long TTL is fine.
LOGO_TTL_SECONDS = 86400.0
LOGO_MAX_ENTRIES = 64
_logo_cache: dict[str, tuple[float, str, bytes]] = {}
_LOGO_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _cookies() -> dict:
    s2, swid = os.environ.get("ESPN_S2", "").strip(), os.environ.get("ESPN_SWID", "").strip()
    return {"espn_s2": s2, "SWID": swid} if s2 and swid else {}


def _roster(raw_side: dict) -> list[dict]:
    roster = (
        raw_side.get("rosterForCurrentMatchupPeriod")
        or raw_side.get("rosterForMatchupPeriod")
        or {}
    )
    players = []
    for e in roster.get("entries") or []:
        slot = e.get("lineupSlotId")
        player = (e.get("playerPoolEntry") or {}).get("player") or {}
        actual, proj = _player_points(player)
        players.append({
            "name": player.get("fullName") or "—",
            "slot": SLOT_LABELS.get(slot, "BE" if slot in (20, 21) else ""),
            "bench": slot in (20, 21),
            "points": round(actual, 1) if actual is not None else None,
            "projected": round(proj, 1) if proj is not None else None,
        })
    # Starters first (in slot order roughly), bench after.
    players.sort(key=lambda p: (p["bench"], p["slot"] or "zz"))
    return players


def _team_block(raw_side: dict, teams_by_id: dict) -> dict:
    team = teams_by_id.get(raw_side.get("teamId"), {})
    total = raw_side.get("totalPointsLive")
    if total is None:
        total = raw_side.get("totalPoints") or 0.0
    return {
        "abbrev": team.get("abbrev") or "?",
        "name": _team_name(team),
        "logo": _logo(team.get("logo")),
        "points": round(float(total), 1),
        "projected": (round(float(raw_side["totalProjectedPointsLive"]), 1)
                      if raw_side.get("totalProjectedPointsLive") is not None else None),
        "players": _roster(raw_side),
    }


@router.get("/fantasy/detail")
async def fantasy_detail(league_id: str, season: int, week: int, team_id: int):
    key = f"{league_id}/{season}/{week}/{team_id}"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        async with httpx.AsyncClient(
            timeout=15, headers=HEADERS, cookies=_cookies(), follow_redirects=True
        ) as client:
            response = await client.get(
                LEAGUE_URL.format(season=season, league_id=league_id),
                params=[("view", "mBoxscore"), ("view", "mRoster"),
                        ("view", "mTeam"), ("scoringPeriodId", week)],
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return JSONResponse({"error": f"boxscore fetch failed: {exc}"}, status_code=502)

    teams_by_id = {t.get("id"): t for t in data.get("teams") or []}
    detail: dict = {"error": "matchup not found"}
    for m in data.get("schedule") or []:
        if int(m.get("matchupPeriodId") or 0) != week:
            continue
        home, away = m.get("home") or {}, m.get("away") or {}
        if team_id in (home.get("teamId"), away.get("teamId")):
            detail = {
                "home": _team_block(home, teams_by_id),
                "away": _team_block(away, teams_by_id) if away else None,
            }
            break

    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[key] = (now, detail)
    return detail


@router.get("/fantasy/logo")
async def fantasy_logo(id: str):
    """Proxy an auth-only ESPN custom team logo so the cookieless kiosk can show
    it. Host- and id-locked (no open proxy); refetched with the server cookies."""
    if not _LOGO_ID_RE.match(id):
        return JSONResponse({"error": "bad image id"}, status_code=400)
    now = time.monotonic()
    cached = _logo_cache.get(id)
    if cached and now - cached[0] < LOGO_TTL_SECONDS:
        return Response(content=cached[2], media_type=cached[1])
    try:
        async with httpx.AsyncClient(
            timeout=15, headers=HEADERS, cookies=_cookies(), follow_redirects=True
        ) as client:
            r = await client.get(LOGO_IMAGE_PREFIX + id)
            r.raise_for_status()
    except Exception as exc:
        return JSONResponse({"error": f"logo fetch failed: {exc}"}, status_code=502)
    content_type = r.headers.get("content-type", "image/png").split(";")[0]
    data = r.content
    if len(_logo_cache) >= LOGO_MAX_ENTRIES:
        _logo_cache.pop(min(_logo_cache, key=lambda k: _logo_cache[k][0]))
    _logo_cache[id] = (now, content_type, data)
    return Response(content=data, media_type=content_type)
