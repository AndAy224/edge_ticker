"""ADS-B "overhead now" + flight-radar collector.

Default source is a free, keyless hosted community ADS-B API (airplanes.live) —
no receiver hardware required. Switch with ``modules.adsb.provider``:

  airplaneslive | adsblol | adsbfi | local

The hosted providers all return an ADSBexchange-v2 shape (an ``ac`` array of
dump1090-style aircraft) and take their radius in nautical miles. ``local``
reads ``ADSB_URL`` from .env (e.g. http://pi-adsb:8080/data/aircraft.json) from
your own dump1090/readsb receiver. Receiver/center position defaults to the
weather module's coordinates.
"""
from __future__ import annotations

import math
import os

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

EARTH_RADIUS_KM = 6371.0
KM_PER_NM = 1.852
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
USER_AGENT = "edge-ticker/1.0 (hobby flight-radar kiosk)"

# Hosted community ADS-B providers — keyless, radius in nautical miles. They all
# return the same ADSBexchange-v2 JSON (an `ac` array), so one parser fits all.
HOSTED_PROVIDERS = {
    "airplaneslive": lambda lat, lon, nm: f"https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
    "adsblol": lambda lat, lon, nm: f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}",
    "adsbfi": lambda lat, lon, nm: f"https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}",
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass_octant(deg: float) -> str:
    return COMPASS[round(deg / 45) % 8]


class AdsbCollector(Collector):
    name = "adsb"
    enabled_by_default = False
    # Hosted providers need no env; the `local` provider reads ADSB_URL itself.
    required_env = ()

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 15))
        self.provider = (self.module_config.get("provider") or "airplaneslive").lower()
        weather = config.get("modules", {}).get("weather", {})
        self.latitude = self.module_config.get("latitude") or weather.get("latitude", 27.9659)
        self.longitude = self.module_config.get("longitude") or weather.get("longitude", -82.8001)
        self.radius_km = float(self.module_config.get("radius_km", 40))
        self.local_url = os.environ.get("ADSB_URL")

    def _url(self) -> str:
        if self.provider == "local":
            if not self.local_url:
                raise RuntimeError(
                    "adsb provider 'local' needs ADSB_URL in .env "
                    "(e.g. http://pi-adsb:8080/data/aircraft.json)"
                )
            return self.local_url
        builder = HOSTED_PROVIDERS.get(self.provider)
        if builder is None:
            raise RuntimeError(f"unknown adsb provider {self.provider!r}")
        nm = max(1, round(self.radius_km / KM_PER_NM))
        return builder(self.latitude, self.longitude, nm)

    async def fetch(self) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            response = await client.get(self._url())
        response.raise_for_status()
        return response.json()

    def shape(self, raw: dict) -> ModulePayload:
        # Hosted providers key the list under "ac"; local dump1090 uses "aircraft".
        raw_list = raw.get("ac") or raw.get("aircraft") or []
        aircraft = []
        for plane in raw_list:
            lat, lon = plane.get("lat"), plane.get("lon")
            if lat is None or lon is None:
                continue
            distance = haversine_km(self.latitude, self.longitude, lat, lon)
            if distance > self.radius_km:
                continue
            bearing = bearing_deg(self.latitude, self.longitude, lat, lon)
            alt_raw = plane.get("alt_baro")
            on_ground = alt_raw == "ground"
            if on_ground:
                alt_ft = 0
            elif isinstance(alt_raw, (int, float)):
                alt_ft = alt_raw
            else:
                alt_ft = plane.get("alt_geom")
            vert = plane.get("baro_rate")
            if vert is None:
                vert = plane.get("geom_rate")
            aircraft.append(
                {
                    "hex": plane.get("hex"),
                    "flight": (plane.get("flight") or "").strip()
                    or plane.get("r")
                    or plane.get("hex", "?"),
                    "alt_ft": alt_ft,
                    "speed_kt": plane.get("gs"),
                    "track": plane.get("track"),
                    "distance_km": round(distance, 1),
                    "bearing_deg": round(bearing, 1),
                    "direction": compass_octant(bearing),
                    "lat": lat,
                    "lon": lon,
                    "type": plane.get("t"),
                    "registration": plane.get("r"),
                    "squawk": plane.get("squawk"),
                    "category": plane.get("category"),
                    "vert_rate": vert,
                    "on_ground": on_ground,
                    # airplanes.live enriches inline (other providers may omit).
                    "desc": plane.get("desc"),
                    "operator": plane.get("ownOp"),
                }
            )
        aircraft.sort(key=lambda a: a["distance_km"])

        tape = [
            TapeItem(
                text=f"{a['flight']} · {a['alt_ft'] or '?'} ft · "
                f"{a['distance_km']:.0f} km {a['direction']}"
            )
            for a in aircraft[:3]
        ]
        total = raw.get("total")
        if total is None:
            total = len(raw_list)
        return ModulePayload(
            module=self.name,
            stage={
                "aircraft": aircraft[:30],
                "count_in_radius": len(aircraft),
                "count_total": total,
                "radius_km": self.radius_km,
            },
            tape=tape,
        )
