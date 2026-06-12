"""Astro conditions collector (stretch) — tonight's cloud cover by layer
(Open-Meteo, keyless), computed moon phase, and a small built-in list of
seasonal imaging targets. Coordinates default to the weather module's.

No key or extra service needed, but disabled by default — enable via config.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

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

# J2000 coordinates (RA in hours, Dec in degrees) for the target catalog.
TARGETS: dict[str, tuple[float, float]] = {
    "M42 Orion Nebula": (5.588, -5.39),
    "M45 Pleiades": (3.790, 24.12),
    "M31 Andromeda": (0.712, 41.27),
    "M81/M82 Bode's": (9.926, 69.07),
    "Rosette Nebula": (6.553, 4.95),
    "M44 Beehive": (8.673, 19.67),
    "Leo Triplet": (11.337, 13.00),
    "M51 Whirlpool": (13.498, 47.20),
    "M104 Sombrero": (12.666, -11.62),
    "M13 Hercules": (16.695, 36.46),
    "M101 Pinwheel": (14.053, 54.35),
    "M57 Ring Nebula": (18.893, 33.03),
    "M8 Lagoon": (18.060, -24.38),
    "M20 Trifid": (18.045, -22.97),
    "M27 Dumbbell": (19.991, 22.72),
    "NGC 7000 N. America": (20.976, 44.33),
    "M33 Triangulum": (1.564, 30.66),
    "Double Cluster": (2.337, 57.14),
    "Heart & Soul": (2.555, 61.45),
    "California Nebula": (4.055, 36.42),
}

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

SIDEREAL_RATE = 1.0027379093  # sidereal hours per solar hour
ALT_THRESHOLD_DEG = 40.0


def _gmst_hours(dt: datetime) -> float:
    """Greenwich mean sidereal time, in hours (good to seconds — display use)."""
    jd = dt.timestamp() / 86400 + 2440587.5
    return (18.697374558 + 24.06570982441908 * (jd - 2451545.0)) % 24


def _transit_near(ra_hours: float, longitude: float, around: datetime) -> datetime:
    """UTC time nearest `around` when the target crosses the local meridian."""
    lst = (_gmst_hours(around) + longitude / 15) % 24
    delta_sidereal = ((ra_hours - lst + 12) % 24) - 12
    return around + timedelta(hours=delta_sidereal / SIDEREAL_RATE)


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

    def _target_times(self, name: str, midpoint: datetime) -> dict:
        """Meridian transit + the window above ALT_THRESHOLD_DEG for tonight."""
        ra_hours, dec_deg = TARGETS[name]
        transit = _transit_near(ra_hours, self.longitude, midpoint)
        lat = math.radians(self.latitude)
        dec = math.radians(dec_deg)
        alt = math.radians(ALT_THRESHOLD_DEG)
        cos_h = (math.sin(alt) - math.sin(lat) * math.sin(dec)) / (
            math.cos(lat) * math.cos(dec)
        )
        entry: dict = {
            "name": name,
            "transit": transit.isoformat(),
            "above40_from": None,
            "above40_until": None,
            "max_alt": None,
            "always_above": False,
        }
        if cos_h >= 1:  # culminates below the threshold
            entry["max_alt"] = round(90 - abs(self.latitude - dec_deg))
        elif cos_h <= -1:  # never dips below it
            entry["always_above"] = True
        else:
            half = timedelta(hours=math.degrees(math.acos(cos_h)) / 15 / SIDEREAL_RATE)
            entry["above40_from"] = (transit - half).isoformat()
            entry["above40_until"] = (transit + half).isoformat()
        return entry

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

        # Night midpoint anchors the transit search (Open-Meteo returns local
        # naive ISO; utc_offset_seconds converts to UTC).
        offset = timedelta(seconds=raw.get("utc_offset_seconds", 0))
        if sunset and sunrise_tomorrow:
            set_local = datetime.fromisoformat(sunset)
            rise_local = datetime.fromisoformat(sunrise_tomorrow)
            midpoint = (set_local + (rise_local - set_local) / 2 - offset).replace(
                tzinfo=timezone.utc
            )
        else:
            midpoint = now + timedelta(hours=3)
        targets = [
            self._target_times(name, midpoint)
            for name in MONTHLY_TARGETS.get(now.month, [])
            if name in TARGETS
        ]

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
