"""ADS-B "overhead now" collector (stretch) — reads dump1090's aircraft.json.

Needs ADSB_URL in .env (e.g. http://pi-adsb:8080/data/aircraft.json); skipped
when absent. Receiver position defaults to the weather module's coordinates.
"""
from __future__ import annotations

import math
import os

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

EARTH_RADIUS_KM = 6371.0
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_compass(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    degrees = (math.degrees(math.atan2(x, y)) + 360) % 360
    return COMPASS[round(degrees / 45) % 8]


class AdsbCollector(Collector):
    name = "adsb"
    enabled_by_default = False
    required_env = ("ADSB_URL",)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 15))
        self.url = os.environ["ADSB_URL"]
        weather = config.get("modules", {}).get("weather", {})
        self.latitude = self.module_config.get("latitude") or weather.get("latitude", 27.9659)
        self.longitude = self.module_config.get("longitude") or weather.get("longitude", -82.8001)
        self.radius_km = float(self.module_config.get("radius_km", 40))

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(self.url)
        response.raise_for_status()
        return response.json()

    def shape(self, raw: dict) -> ModulePayload:
        aircraft = []
        for plane in raw.get("aircraft", []):
            lat, lon = plane.get("lat"), plane.get("lon")
            if lat is None or lon is None:
                continue
            distance = haversine_km(self.latitude, self.longitude, lat, lon)
            if distance > self.radius_km:
                continue
            aircraft.append(
                {
                    "hex": plane.get("hex"),
                    "flight": (plane.get("flight") or "").strip() or plane.get("hex", "?"),
                    "alt_ft": plane.get("alt_baro") or plane.get("alt_geom"),
                    "speed_kt": plane.get("gs"),
                    "track": plane.get("track"),
                    "distance_km": round(distance, 1),
                    "direction": bearing_compass(self.latitude, self.longitude, lat, lon),
                }
            )
        aircraft.sort(key=lambda a: a["distance_km"])

        tape = [
            TapeItem(
                text=f"✈ {a['flight']} {a['alt_ft'] or '?'} ft · "
                f"{a['distance_km']:.0f} km {a['direction']}"
            )
            for a in aircraft[:3]
        ]
        return ModulePayload(
            module=self.name,
            stage={
                "aircraft": aircraft[:8],
                "count_in_radius": len(aircraft),
                "count_total": len(raw.get("aircraft", [])),
                "radius_km": self.radius_km,
            },
            tape=tape,
        )
