"""Weather-radar collector — RainViewer public weather-maps API (keyless).

Publishes the available radar frame list (past ~2h at 10-minute steps plus any
short-term nowcast frames); the display fetches the actual PNG tiles itself,
same as it does for team logos. Frame paths are immutable so the browser cache
absorbs the tile traffic. Reuses the weather module's coordinates (the admin
labels them as the shared receiver position for adsb/astro).

Has a stage page; the rotation order in config simply has to include it."""
from __future__ import annotations

import httpx

from ..state import ModulePayload
from .base import Collector

WEATHER_MAPS_URL = "https://api.rainviewer.com/public/weather-maps.json"

# Keep the loop short enough to read on a 25s rotation dwell: the newest
# frames matter most, so cap the past list and take every nowcast frame.
MAX_PAST_FRAMES = 10


class WeatherRadarCollector(Collector):
    name = "weather_radar"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 300))
        weather = config.get("modules", {}).get("weather", {})
        self.location_name = self.module_config.get(
            "location_name", weather.get("location_name", "Clearwater, FL")
        )
        self.latitude = self.module_config.get(
            "latitude", weather.get("latitude", 27.9659)
        )
        self.longitude = self.module_config.get(
            "longitude", weather.get("longitude", -82.8001)
        )
        # Fractional zooms work: the display renders the nearest integer tile
        # level and CSS-scales the map (7 ≈ regional, 8 ≈ metro).
        self.zoom = float(self.module_config.get("zoom", 7.5))
        # RainViewer palette id (4 = The Weather Channel colors).
        self.color_scheme = int(self.module_config.get("color_scheme", 4))

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(WEATHER_MAPS_URL)
        response.raise_for_status()
        return response.json()

    def shape(self, raw: dict) -> ModulePayload:
        radar = raw.get("radar") or {}
        frames = [
            {"time": frame.get("time"), "path": frame.get("path"), "nowcast": False}
            for frame in (radar.get("past") or [])[-MAX_PAST_FRAMES:]
        ] + [
            {"time": frame.get("time"), "path": frame.get("path"), "nowcast": True}
            for frame in (radar.get("nowcast") or [])
        ]
        frames = [f for f in frames if f["time"] and f["path"]]
        return ModulePayload(
            module=self.name,
            stage={
                "host": raw.get("host", "https://tilecache.rainviewer.com"),
                "frames": frames,
                "center": {"lat": self.latitude, "lon": self.longitude},
                "zoom": self.zoom,
                "color": self.color_scheme,
                "location_name": self.location_name,
            },
            tape=[],
        )
