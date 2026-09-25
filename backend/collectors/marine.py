"""Beach & tides — NOAA CO-OPS tide predictions + water temperature, Open-Meteo
marine (waves, sea-surface temperature) and UV. All keyless.

The tide station is the nearest NOAA *reference* station to home (only those
publish an hourly prediction curve; subordinate stations are highs/lows only),
unless modules.marine.station pins one. Predictions are requested in GMT and
published as UTC ISO strings, so the display never has to guess a timezone.

Predictions change only with the calendar, so they are re-fetched every few
hours; water temperature, waves and UV ride every poll. Only the tide curve is
required — the rest degrade individually.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREDICTIONS_REFRESH_SECONDS = 6 * 3600
CURVE_HOURS_BACK = 6
CURVE_HOURS_AHEAD = 30
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _now() -> datetime:
    return datetime.now(timezone.utc)  # a function so tests can pin the clock


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _gmt(stamp: str) -> datetime:
    """NOAA 'YYYY-MM-DD HH:MM' (requested in GMT) → aware UTC datetime."""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%I:%M %p").lstrip("0")


def nearest_reference_station(stations: list[dict], lat: float, lon: float) -> dict | None:
    candidates = [s for s in stations if s.get("type") == "R" and s.get("lat") is not None]
    if not candidates:
        return None
    best = min(candidates, key=lambda s: _km(lat, lon, s["lat"], s["lng"]))
    return {
        "id": str(best["id"]),
        "name": best.get("name"),
        "lat": best["lat"],
        "lon": best["lng"],
        "distance_km": round(_km(lat, lon, best["lat"], best["lng"]), 1),
    }


def tide_now(curve: list[dict], now: datetime) -> float | None:
    """Height at `now`, linearly interpolated along the hourly curve."""
    for a, b in zip(curve, curve[1:]):
        ta, tb = a["_dt"], b["_dt"]
        if ta <= now <= tb:
            frac = (now - ta).total_seconds() / max((tb - ta).total_seconds(), 1)
            return a["v"] + (b["v"] - a["v"]) * frac
    return None


class MarineCollector(Collector):
    name = "marine"
    uses_location = True
    # Only useful near a coast — ships off, like the other stretch modules.
    enabled_by_default = False

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 1800))
        self.latitude, self.longitude, self.location_name = self.home()
        self.station_override = self.module_config.get("station")
        self._station: dict | None = None
        self._predictions: dict | None = None  # {"curve": [...], "events": [...]}
        self._predictions_at = 0.0
        self._predictions_day = ""

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "edge-ticker/1.0"}) as client:
            station = await self._resolve_station(client)
            await self._refresh_predictions(client, station)
            water, marine, uv = await asyncio.gather(
                self._water_temp(client, station),
                self._marine(client, station),
                self._uv(client),
                return_exceptions=True,
            )
        problems = [
            f"{label}: {type(r).__name__}"
            for label, r in (("water temperature", water), ("waves", marine), ("uv", uv))
            if isinstance(r, BaseException)
        ]
        self.degraded = "; ".join(problems) or None
        return {
            "station": station,
            "predictions": self._predictions,
            "water": None if isinstance(water, BaseException) else water,
            "marine": None if isinstance(marine, BaseException) else marine,
            "uv": None if isinstance(uv, BaseException) else uv,
        }

    async def _resolve_station(self, client: httpx.AsyncClient) -> dict:
        if self._station is not None:
            return self._station
        if self.station_override:
            r = await client.get(f"{STATIONS_URL[:-len('.json')]}/{self.station_override}.json")
            r.raise_for_status()
            s = (r.json().get("stations") or [{}])[0]
            self._station = {
                "id": str(self.station_override),
                "name": s.get("name"),
                "lat": s.get("lat"),
                "lon": s.get("lng"),
                "distance_km": round(_km(self.latitude, self.longitude, s["lat"], s["lng"]), 1)
                if s.get("lat") is not None else None,
            }
        else:
            # ~2 MB, once per collector lifetime.
            r = await client.get(STATIONS_URL, params={"type": "tidepredictions"})
            r.raise_for_status()
            station = nearest_reference_station(r.json().get("stations") or [], self.latitude, self.longitude)
            if station is None:
                raise RuntimeError("no NOAA reference tide station found")
            self._station = station
        return self._station

    async def _refresh_predictions(self, client: httpx.AsyncClient, station: dict) -> None:
        today = datetime.now().date().isoformat()
        fresh = (
            self._predictions is not None
            and self._predictions_day == today
            and time.monotonic() - self._predictions_at < PREDICTIONS_REFRESH_SECONDS
        )
        if fresh:
            return
        begin = (datetime.now(timezone.utc) - timedelta(hours=CURVE_HOURS_BACK + 12)).strftime("%Y%m%d %H:%M")
        common = {
            "station": station["id"],
            "product": "predictions",
            "datum": "MLLW",
            "time_zone": "gmt",
            "units": "english",
            "format": "json",
            "application": "edge-ticker",
            "begin_date": begin,
            "range": str(CURVE_HOURS_BACK + CURVE_HOURS_AHEAD + 36),
        }
        hourly, hilo = await asyncio.gather(
            client.get(DATA_URL, params={**common, "interval": "h"}),
            client.get(DATA_URL, params={**common, "interval": "hilo"}),
        )
        for r in (hourly, hilo):
            r.raise_for_status()
            if "error" in r.json():
                raise RuntimeError(f"NOAA: {r.json()['error'].get('message')}")
        self._predictions = {
            "curve": [{"t": p["t"], "v": float(p["v"])} for p in hourly.json().get("predictions", [])],
            "events": [
                {"t": p["t"], "v": float(p["v"]), "type": p.get("type")}
                for p in hilo.json().get("predictions", [])
            ],
        }
        self._predictions_at = time.monotonic()
        self._predictions_day = today

    async def _water_temp(self, client: httpx.AsyncClient, station: dict) -> dict | None:
        r = await client.get(DATA_URL, params={
            "station": station["id"], "product": "water_temperature", "date": "latest",
            "time_zone": "gmt", "units": "english", "format": "json", "application": "edge-ticker",
        })
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data or data[-1].get("v") in (None, ""):
            return None  # this station has no temperature sensor
        return {"f": float(data[-1]["v"]), "at": _iso(_gmt(data[-1]["t"]))}

    async def _marine(self, client: httpx.AsyncClient, station: dict) -> dict:
        r = await client.get(MARINE_URL, params={
            # The beach, not home: the marine grid snaps to the nearest sea cell.
            "latitude": station.get("lat") or self.latitude,
            "longitude": station.get("lon") or self.longitude,
            "current": "wave_height,wave_period,wave_direction,sea_surface_temperature",
            "length_unit": "imperial",
            "temperature_unit": "fahrenheit",
        })
        r.raise_for_status()
        return r.json().get("current") or {}

    async def _uv(self, client: httpx.AsyncClient) -> dict:
        r = await client.get(FORECAST_URL, params={
            "latitude": self.latitude,
            "longitude": self.longitude,
            "current": "uv_index",
            "daily": "uv_index_max",
            "timezone": "auto",
            "forecast_days": 1,
        })
        r.raise_for_status()
        data = r.json()
        return {
            "now": (data.get("current") or {}).get("uv_index"),
            "max": ((data.get("daily") or {}).get("uv_index_max") or [None])[0],
        }

    def shape(self, raw: dict) -> ModulePayload:
        now = _now()
        predictions = raw.get("predictions") or {"curve": [], "events": []}
        curve = [{**p, "_dt": _gmt(p["t"])} for p in predictions["curve"]]
        window = [
            p for p in curve
            if now - timedelta(hours=CURVE_HOURS_BACK) <= p["_dt"] <= now + timedelta(hours=CURVE_HOURS_AHEAD)
        ]
        events = [{**e, "_dt": _gmt(e["t"])} for e in predictions["events"]]
        upcoming = [e for e in events if e["_dt"] > now]
        previous = [e for e in events if e["_dt"] <= now]
        height = tide_now(curve, now)
        next_event = upcoming[0] if upcoming else None

        def out(e: dict) -> dict:
            return {"t": _iso(e["_dt"]), "ft": round(e["v"], 2), "type": e.get("type")}

        water = raw.get("water")
        marine = raw.get("marine") or {}
        water_f, water_source = (water["f"], "station") if water else (
            (marine.get("sea_surface_temperature"), "model")
            if marine.get("sea_surface_temperature") is not None else (None, None)
        )
        direction = marine.get("wave_direction")
        waves = None
        if marine.get("wave_height") is not None:
            waves = {
                "ft": round(marine["wave_height"], 1),
                "period_s": marine.get("wave_period"),
                # Waves are reported by where they come *from*.
                "from": COMPASS[round(direction / 22.5) % 16] if direction is not None else None,
            }
        uv = raw.get("uv") or {}
        station = raw.get("station") or {}

        tape_bits = []
        if next_event:
            word = "high" if next_event.get("type") == "H" else "low"
            tape_bits.append(f"{word} tide {_local_time(next_event['_dt'])} ({next_event['v']:.1f} ft)")
        if water_f is not None:
            tape_bits.append(f"water {round(water_f)}°")
        if waves:
            tape_bits.append(f"waves {waves['ft']:g} ft")
        if uv.get("max") is not None:
            tape_bits.append(f"UV max {round(uv['max'])}")
        tape = [TapeItem(text=f"{station.get('name') or 'Beach'}: " + " · ".join(tape_bits))] if tape_bits else []

        return ModulePayload(
            module=self.name,
            stage={
                "station": station,
                "location_name": self.location_name,
                "tide": {
                    "curve": [{"t": _iso(p["_dt"]), "ft": round(p["v"], 2)} for p in window],
                    "events": [out(e) for e in (previous[-1:] + upcoming[:4])],
                    "now_ft": round(height, 2) if height is not None else None,
                    "rising": (next_event or {}).get("type") == "H" if next_event else None,
                    "next": out(next_event) if next_event else None,
                },
                "water_f": round(water_f, 1) if water_f is not None else None,
                "water_source": water_source,
                "waves": waves,
                "uv": {"now": uv.get("now"), "max": uv.get("max")},
            },
            tape=tape,
        )
