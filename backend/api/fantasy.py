"""On-demand fantasy boxscore for the display's tap-to-expand view.

The collector's payload carries only the starting lineups; tapping a matchup
fetches the full boxscore (starters + bench, projected vs actual per player)
here, with a small TTL cache so repeated taps don't hammer ESPN. Never polled.
"""
from __future__ import annotations

import asyncio
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
    POS_LABELS,
    SLOT_LABELS,
    UNAVAILABLE,
    _injury_badge,
    _logo,
    _player_points,
    _team_name,
    player_meta,
    pro_teams,
)

router = APIRouter()

CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 50
_cache: dict[str, tuple[float, dict]] = {}

PLAYER_TTL_SECONDS = 300.0
PLAYER_MAX_ENTRIES = 100
_player_cache: dict[str, tuple[float, dict]] = {}

# Logo proxy cache: image id -> (monotonic_time, content_type, bytes). Logos
# rarely change, so a long TTL is fine.
LOGO_TTL_SECONDS = 86400.0
LOGO_MAX_ENTRIES = 64
_logo_cache: dict[str, tuple[float, str, bytes]] = {}
_LOGO_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _cookies() -> dict:
    s2, swid = os.environ.get("ESPN_S2", "").strip(), os.environ.get("ESPN_SWID", "").strip()
    return {"espn_s2": s2, "SWID": swid} if s2 and swid else {}


def _entry_player(e: dict, pro: dict, week: int) -> dict:
    slot = e.get("lineupSlotId")
    ppe = e.get("playerPoolEntry") or {}
    player = ppe.get("player") or {}
    actual, proj = _player_points(player)
    return {
        "name": player.get("fullName") or "—",
        "playerId": ppe.get("id") or player.get("id"),
        "slot": SLOT_LABELS.get(slot, "IR" if slot == 21 else "BE" if slot == 20 else ""),
        "lineupSlotId": slot,
        "eligible": player.get("eligibleSlots") or [],
        "bench": slot in (20, 21),
        "points": round(actual, 1) if actual is not None else None,
        "projected": round(proj, 1) if proj is not None else None,
        **player_meta(player, pro, week),
    }


def _roster(raw_side: dict, pro: dict, week: int) -> list[dict]:
    """Matchup-side lineup (per-week points). Slots are reliable only live."""
    roster = (
        raw_side.get("rosterForCurrentMatchupPeriod")
        or raw_side.get("rosterForMatchupPeriod")
        or {}
    )
    players = [_entry_player(e, pro, week) for e in roster.get("entries") or []]
    players.sort(key=lambda p: (p["bench"], p["slot"] or "zz"))
    return players


def _team_roster(team: dict, pro: dict, week: int) -> list[dict]:
    """Current full roster (correct slots + bench/IR + eligibility) for the
    health board — from team.roster.entries, independent of matchup history."""
    entries = (team.get("roster") or {}).get("entries") or []
    players = [_entry_player(e, pro, week) for e in entries]

    def order(p: dict):
        s = p["lineupSlotId"]
        group = 2 if s == 21 else 1 if s == 20 else 0  # starters, bench, IR
        return (group, s if s is not None else 99)

    players.sort(key=order)
    return players


def _attention(players: list[dict]) -> list[dict]:
    """Unavailable starters + the best healthy bench player who can fill the slot."""
    healthy_bench = [
        p for p in players
        if p["lineupSlotId"] == 20 and not p["bye"] and not p["injured"]
        and p["injury"] not in UNAVAILABLE
    ]
    out = []
    for s in players:
        if s["bench"]:
            continue
        if not (s["bye"] or s["injured"] or s["injury"] in UNAVAILABLE):
            continue
        reason = "BYE" if s["bye"] else (s["injury"] or "OUT")
        cands = [b for b in healthy_bench if s["lineupSlotId"] in (b["eligible"] or [])]
        cands.sort(key=lambda b: -(b["projected"] or 0))
        suggest = cands[0] if cands else None
        out.append({
            "out": {"name": s["name"], "slot": s["slot"], "reason": reason},
            "suggest": ({"name": suggest["name"], "pos": suggest["pos"],
                         "proj": suggest["projected"]} if suggest else None),
        })
    return out


async def _week_points(client, league_id: str, season: int, wk: int, player_id: int) -> dict | None:
    """One week's (points, projected) for a player — past-week rosters need a
    per-scoringPeriod fetch (the season-wide boxscore only carries the current week)."""
    try:
        r = await client.get(
            LEAGUE_URL.format(season=season, league_id=league_id),
            params=[("view", "mBoxscore"), ("scoringPeriodId", wk)],
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    for m in data.get("schedule") or []:
        if int(m.get("matchupPeriodId") or 0) != wk:
            continue
        for side in ("home", "away"):
            roster = (
                (m.get(side) or {}).get("rosterForMatchupPeriod")
                or (m.get(side) or {}).get("rosterForCurrentMatchupPeriod")
                or {}
            )
            for e in roster.get("entries") or []:
                ppe = e.get("playerPoolEntry") or {}
                if (ppe.get("id") or (ppe.get("player") or {}).get("id")) != player_id:
                    continue
                actual, proj = _player_points(ppe.get("player") or {})
                return {
                    "week": wk,
                    "points": round(actual, 1) if actual is not None else None,
                    "projected": round(proj, 1) if proj is not None else None,
                }
    return None


def _team_block(raw_side: dict, teams_by_id: dict, pro: dict, week: int) -> dict:
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
        "players": _roster(raw_side, pro, week),
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
            pro = await pro_teams(season)
    except Exception as exc:
        return JSONResponse({"error": f"boxscore fetch failed: {exc}"}, status_code=502)

    teams_by_id = {t.get("id"): t for t in data.get("teams") or []}
    # Health board uses the team's CURRENT roster (correct slots + bench + eligibility).
    roster = _team_roster(teams_by_id.get(team_id, {}), pro, week)
    detail: dict = {"error": "matchup not found", "roster": roster, "attention": _attention(roster)}
    for m in data.get("schedule") or []:
        if int(m.get("matchupPeriodId") or 0) != week:
            continue
        home, away = m.get("home") or {}, m.get("away") or {}
        if team_id in (home.get("teamId"), away.get("teamId")):
            detail = {
                "home": _team_block(home, teams_by_id, pro, week),
                "away": _team_block(away, teams_by_id, pro, week) if away else None,
                "roster": roster,
                "attention": _attention(roster),
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


@router.get("/fantasy/player")
async def fantasy_player(league_id: str, season: int, week: int, player_id: int):
    """Per-player card: identity, injury, ownership, this-week opp/bye/projected,
    season total/avg, and a weekly points log for the sparkline."""
    key = f"{league_id}/{season}/{week}/{player_id}"
    now = time.monotonic()
    cached = _player_cache.get(key)
    if cached and now - cached[0] < PLAYER_TTL_SECONDS:
        return cached[1]
    player_filter = '{"players":{"filterIds":{"value":[%d]}}}' % player_id
    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, cookies=_cookies(), follow_redirects=True
        ) as client:
            r1 = await client.get(
                LEAGUE_URL.format(season=season, league_id=league_id),
                params=[("view", "kona_player_info")],
                headers={"X-Fantasy-Filter": player_filter},
            )
            r1.raise_for_status()
            players = r1.json().get("players") or []
            info = (players[0].get("player") if players else {}) or {}
            # Weekly game log: one fetch per past week (parallel), capped at ~8.
            weeks = list(range(max(1, week - 8), week + 1))
            results = await asyncio.gather(
                *(_week_points(client, league_id, season, wk, player_id) for wk in weeks)
            )
            weekly = [w for w in results if w]
        pro = await pro_teams(season)
    except Exception as exc:
        return JSONResponse({"error": f"player fetch failed: {exc}"}, status_code=502)

    meta = player_meta(info, pro, week)
    own = info.get("ownership") or {}
    actuals = [w["points"] for w in weekly if w["week"] < week and w["points"] is not None]
    this_proj = next((w.get("projected") for w in weekly if w["week"] == week), None)
    detail = {
        "name": info.get("fullName") or "—",
        "playerId": player_id,
        "pos": meta["pos"] or POS_LABELS.get(info.get("defaultPositionId"), ""),
        "proTeam": meta["proTeam"],
        "jersey": info.get("jersey"),
        "injury": meta["injury"] or _injury_badge(info),
        "injured": meta["injured"],
        "opp": meta["opp"],
        "kickoff": meta["kickoff"],
        "bye": meta["bye"],
        "percentOwned": round(own["percentOwned"], 1) if own.get("percentOwned") is not None else None,
        "percentStarted": round(own["percentStarted"], 1) if own.get("percentStarted") is not None else None,
        "projected": round(this_proj, 1) if this_proj is not None else None,
        "seasonTotal": round(sum(actuals), 1) if actuals else None,
        "seasonAvg": round(sum(actuals) / len(actuals), 1) if actuals else None,
        "weekly": weekly,
    }
    if len(_player_cache) >= PLAYER_MAX_ENTRIES:
        _player_cache.pop(min(_player_cache, key=lambda k: _player_cache[k][0]))
    _player_cache[key] = (now, detail)
    return detail
