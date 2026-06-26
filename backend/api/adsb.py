"""On-demand flight-route lookup for the ADS-B radar's tap-to-expand readout.

ADS-B transponders broadcast position/altitude/callsign but NOT a flight's
origin/destination. We resolve the callsign against adsbdb.com (free, keyless)
on tap only — never polled — with a small TTL cache so repeated taps and the
display's re-renders don't hammer the upstream. General-aviation / private
callsigns have no published route (adsbdb 404s); we cache those misses briefly
so a quiet GA-heavy sky doesn't re-query every tap.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter

router = APIRouter()

ADSBDB = "https://api.adsbdb.com/v0"
USER_AGENT = "edge-ticker/1.0 (hobby flight-radar kiosk)"
CACHE_TTL_SECONDS = 3600.0  # a flight's route is static for its whole life
NEGATIVE_TTL_SECONDS = 600.0  # GA/unknown callsigns 404 — don't re-hammer
ERROR_TTL_SECONDS = 60.0  # transient upstream failure — retry sooner
CACHE_MAX_ENTRIES = 200
_cache: dict[str, tuple[float, float, dict]] = {}  # callsign -> (ts, ttl, data)


def _airport(node: dict | None) -> dict | None:
    if not isinstance(node, dict):
        return None
    return {
        "iata": node.get("iata_code"),
        "icao": node.get("icao_code"),
        "city": node.get("municipality"),
        "name": node.get("name"),
        "country": node.get("country_name"),
    }


@router.get("/adsb/route")
async def adsb_route(callsign: str):
    callsign = callsign.strip().upper()
    if not callsign:
        return {"route": None}

    now = time.monotonic()
    cached = _cache.get(callsign)
    if cached and now - cached[0] < cached[1]:
        return cached[2]

    data: dict = {"route": None}
    ttl = NEGATIVE_TTL_SECONDS
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ) as client:
            response = await client.get(f"{ADSBDB}/callsign/{callsign}")
        if response.status_code == 200:
            route = (response.json().get("response") or {}).get("flightroute") or {}
            origin = _airport(route.get("origin"))
            destination = _airport(route.get("destination"))
            if origin or destination:
                airline = route.get("airline")
                data = {
                    "route": {
                        "origin": origin,
                        "destination": destination,
                        "airline": airline.get("name")
                        if isinstance(airline, dict)
                        else airline,
                    }
                }
                ttl = CACHE_TTL_SECONDS
    except httpx.HTTPError:
        ttl = ERROR_TTL_SECONDS

    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[callsign] = (now, ttl, data)
    return data
