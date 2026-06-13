"""Air-quality collector — Open-Meteo Air-Quality API (keyless). Mirrors the
weather collector: same provider style, same stage+tape shape. Reuses the
weather module's coordinates (the admin labels them as the shared receiver
position for adsb/astro), so it needs no location config of its own.

Has a stage page; the rotation order in config simply has to include it."""
from __future__ import annotations

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# US AQI bands → (category label, tape accent). 0-based lower bounds.
# https://www.airnow.gov/aqi/aqi-basics/
AQI_BANDS = [
    (50, "Good", "up"),
    (100, "Moderate", "neutral"),
    (150, "Unhealthy for sensitive groups", "alert"),
    (200, "Unhealthy", "down"),
    (300, "Very unhealthy", "down"),
    (10_000, "Hazardous", "down"),
]

# Concentration field → (display label, per-pollutant US-AQI sub-index field).
# The sub-index lets us name the dominant pollutant without re-deriving AQI.
POLLUTANTS = [
    ("pm2_5", "PM2.5", "us_aqi_pm2_5"),
    ("pm10", "PM10", "us_aqi_pm10"),
    ("ozone", "O₃", "us_aqi_ozone"),
    ("nitrogen_dioxide", "NO₂", "us_aqi_nitrogen_dioxide"),
    ("sulphur_dioxide", "SO₂", "us_aqi_sulphur_dioxide"),
    ("carbon_monoxide", "CO", "us_aqi_carbon_monoxide"),
]

# Open-Meteo pollen fields (Europe-only data; null elsewhere) → display label.
POLLEN = [
    ("grass_pollen", "Grass"),
    ("birch_pollen", "Birch"),
    ("ragweed_pollen", "Ragweed"),
    ("alder_pollen", "Alder"),
    ("mugwort_pollen", "Mugwort"),
    ("olive_pollen", "Olive"),
]


def aqi_band(value: float | int | None) -> tuple[str, str]:
    """Return (category, accent) for a US AQI value."""
    if value is None:
        return ("", "neutral")
    for ceiling, label, accent in AQI_BANDS:
        if value <= ceiling:
            return (label, accent)
    return ("Hazardous", "down")


class AirQualityCollector(Collector):
    name = "airquality"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 1800))
        # Reuse the weather module's location (shared receiver position, like
        # adsb/astro) but allow an explicit override on this module.
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

    async def fetch(self) -> dict:
        current_fields = (
            ["us_aqi"]
            + [c for c, _, _ in POLLUTANTS]
            + [sub for _, _, sub in POLLUTANTS]
            + [p for p, _ in POLLEN]
        )
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                AIR_QUALITY_URL,
                params={
                    "latitude": self.latitude,
                    "longitude": self.longitude,
                    "current": ",".join(current_fields),
                    "hourly": "us_aqi,pm2_5",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
        response.raise_for_status()
        return response.json()

    def shape(self, raw: dict) -> ModulePayload:
        current_raw = raw.get("current", {})
        units = raw.get("current_units", {})
        aqi_value = current_raw.get("us_aqi")
        category, accent = aqi_band(aqi_value)

        # Dominant pollutant = the one whose US-AQI sub-index is highest.
        pollutants = []
        dominant = None
        dominant_sub = -1.0
        for field, label, sub_field in POLLUTANTS:
            conc = current_raw.get(field)
            if conc is None:
                continue
            sub = current_raw.get(sub_field)
            pollutants.append(
                {
                    "key": field,
                    "label": label,
                    "value": conc,
                    "unit": units.get(field, "µg/m³"),
                    "sub_aqi": sub,
                }
            )
            if sub is not None and sub > dominant_sub:
                dominant_sub = sub
                dominant = label

        pollen = [
            {"key": field, "label": label, "value": current_raw.get(field)}
            for field, label in POLLEN
            if current_raw.get(field) is not None
        ]

        aqi = {
            "value": aqi_value,
            "category": category,
            "accent": accent,
            "dominant": dominant,
        }

        # Next 24 hours from the current hour, for the stage AQI graph.
        hourly_raw = raw.get("hourly", {})
        now_hour = (current_raw.get("time") or "")[:13]

        def hour_value(key: str, i: int):
            values = hourly_raw.get(key) or []
            return values[i] if i < len(values) else None

        hourly = []
        for i, t in enumerate(hourly_raw.get("time") or []):
            if now_hour and t[:13] < now_hour:
                continue
            hourly.append(
                {
                    "time": t,
                    "aqi": hour_value("us_aqi", i),
                    "pm2_5": hour_value("pm2_5", i),
                }
            )
            if len(hourly) >= 24:
                break

        tape = []
        if aqi_value is not None:
            text = f"{self.location_name} AQI {round(aqi_value)} — {category}"
            tape.append(TapeItem(text=text, accent="alert" if aqi_value > 100 else "neutral"))

        return ModulePayload(
            module=self.name,
            stage={
                "location": self.location_name,
                "aqi": aqi,
                "pollutants": pollutants,
                "pollen": pollen,
                "hourly": hourly,
            },
            tape=tape,
        )
