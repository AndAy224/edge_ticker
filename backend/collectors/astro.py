"""Astro conditions collector (stretch) — tonight's cloud cover by layer
(Open-Meteo, keyless), computed moon phase, and a small built-in list of
seasonal imaging targets. Coordinates default to the weather module's.

No key or extra service needed, but disabled by default — enable via config.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

SYNODIC_MONTH_DAYS = 29.530588853
# Reference new moon: 2000-01-06 18:14 UTC
NEW_MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

PHASE_NAMES = [
    "New moon",
    "Waxing crescent",
    "First quarter",
    "Waxing gibbous",
    "Full moon",
    "Waning gibbous",
    "Last quarter",
    "Waning crescent",
]

# A few bright, well-placed evening targets per month (northern mid-latitudes).
MONTHLY_TARGETS = {
    1: ["M42 Orion Nebula", "M45 Pleiades", "M31 Andromeda"],
    2: ["M42 Orion Nebula", "M81/M82 Bode's", "Rosette Nebula"],
    3: ["M81/M82 Bode's", "M44 Beehive", "Leo Triplet"],
    4: ["M51 Whirlpool", "M104 Sombrero", "Leo Triplet"],
    5: ["M51 Whirlpool", "M13 Hercules", "M101 Pinwheel"],
    6: ["M13 Hercules", "M57 Ring Nebula", "M51 Whirlpool"],
    7: ["M8 Lagoon", "M20 Trifid", "M57 Ring Nebula"],
    8: ["M31 Andromeda", "M27 Dumbbell", "M8 Lagoon"],
    9: ["M31 Andromeda", "NGC 7000 N. America", "M27 Dumbbell"],
    10: ["M31 Andromeda", "M33 Triangulum", "Double Cluster"],
    11: ["M45 Pleiades", "M31 Andromeda", "Heart & Soul"],
    12: ["M42 Orion Nebula", "M45 Pleiades", "California Nebula"],
}


def moon_phase(now: datetime) -> tuple[str, int]:
    """Phase name + illumination percent from days since a known new moon."""
    age_days = ((now - NEW_MOON_EPOCH).total_seconds() / 86400) % SYNODIC_MONTH_DAYS
    fraction = age_days / SYNODIC_MONTH_DAYS
    name = PHASE_NAMES[int(fraction * 8 + 0.5) % 8]
    illumination = round((1 - math.cos(2 * math.pi * fraction)) / 2 * 100)
    return name, illumination


class AstroCollector(Collector):
    name = "astro"
    enabled_by_default = False

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 1800))
        weather = config.get("modules", {}).get("weather", {})
        self.latitude = self.module_config.get("latitude") or weather.get("latitude", 27.9659)
        self.longitude = self.module_config.get("longitude") or weather.get("longitude", -82.8001)

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                FORECAST_URL,
                params={
                    "latitude": self.latitude,
                    "longitude": self.longitude,
                    "hourly": "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high",
                    "daily": "sunrise,sunset",
                    "timezone": "auto",
                    "forecast_days": 2,
                },
            )
        response.raise_for_status()
        return response.json()

    def shape(self, raw: dict) -> ModulePayload:
        daily = raw.get("daily", {})
        sunset = (daily.get("sunset") or [None])[0]
        sunrise_tomorrow = (daily.get("sunrise") or [None, None])[1]

        hourly = raw.get("hourly", {})
        times = hourly.get("time") or []
        hours = []
        if sunset and sunrise_tomorrow:
            for i, t in enumerate(times):
                if sunset[:13] <= t[:13] <= sunrise_tomorrow[:13]:
                    hours.append(
                        {
                            "time": t,
                            "total": (hourly.get("cloud_cover") or [])[i],
                            "low": (hourly.get("cloud_cover_low") or [])[i],
                            "mid": (hourly.get("cloud_cover_mid") or [])[i],
                            "high": (hourly.get("cloud_cover_high") or [])[i],
                        }
                    )
        covers = [h["total"] for h in hours if h["total"] is not None]
        avg_cloud = round(sum(covers) / len(covers)) if covers else None

        now = datetime.now(timezone.utc)
        phase_name, illumination = moon_phase(now)
        targets = MONTHLY_TARGETS.get(now.month, [])

        tape = []
        if avg_cloud is not None:
            quality = "clear" if avg_cloud < 25 else "mixed" if avg_cloud < 60 else "clouded out"
            tape.append(
                TapeItem(
                    text=f"Astro: {avg_cloud}% cloud tonight ({quality}) · "
                    f"Moon {illumination}% {phase_name}",
                    accent="up" if avg_cloud < 25 else "neutral",
                )
            )
        return ModulePayload(
            module=self.name,
            stage={
                "sunset": sunset,
                "sunrise": sunrise_tomorrow,
                "hours": hours,
                "avg_cloud": avg_cloud,
                "moon": {"phase": phase_name, "illumination": illumination},
                "targets": targets,
            },
            tape=tape,
        )
