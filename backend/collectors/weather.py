"""Weather collector — Open-Meteo (keyless). Feeds the status rail, not a
stage page: the rotation order in config simply never includes it."""
from __future__ import annotations

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes → short display text
WMO_TEXT = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Showers",
    81: "Showers",
    82: "Heavy showers",
    95: "Thunderstorm",
    96: "Storm w/ hail",
    99: "Storm w/ hail",
}


class WeatherCollector(Collector):
    name = "weather"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 900))
        self.location_name = self.module_config.get("location_name", "Clearwater, FL")
        self.latitude = self.module_config.get("latitude", 27.9659)
        self.longitude = self.module_config.get("longitude", -82.8001)

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                FORECAST_URL,
                params={
                    "latitude": self.latitude,
                    "longitude": self.longitude,
                    "current": "temperature_2m,apparent_temperature,"
                    "relative_humidity_2m,weather_code,wind_speed_10m",
                    "hourly": "temperature_2m,precipitation_probability,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,weather_code,sunrise,sunset",
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "timezone": "auto",
                    "forecast_days": 5,
                },
            )
        response.raise_for_status()
        return response.json()

    def shape(self, raw: dict) -> ModulePayload:
        current_raw = raw.get("current", {})
        code = current_raw.get("weather_code")
        current = {
            "temp": current_raw.get("temperature_2m"),
            "feels_like": current_raw.get("apparent_temperature"),
            "humidity": current_raw.get("relative_humidity_2m"),
            "wind": current_raw.get("wind_speed_10m"),
            "code": code,
            "text": WMO_TEXT.get(code, ""),
        }
        daily_raw = raw.get("daily", {})
        blank = [None] * len(daily_raw.get("time", []))
        daily = [
            {
                "date": date,
                "high": daily_raw.get("temperature_2m_max", blank)[i],
                "low": daily_raw.get("temperature_2m_min", blank)[i],
                "precip": daily_raw.get("precipitation_probability_max", blank)[i],
                "code": daily_raw.get("weather_code", blank)[i],
                "text": WMO_TEXT.get(
                    daily_raw.get("weather_code", blank)[i], ""
                ),
            }
            for i, date in enumerate(daily_raw.get("time", []))
        ]
        tape = []
        if current["temp"] is not None:
            tape.append(
                TapeItem(
                    text=f"{self.location_name} {round(current['temp'])}°F "
                    f"{current['text']}".strip()
                )
            )
        # Next 24 hours from the current hour, for the stage forecast graph.
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
                    "temp": hour_value("temperature_2m", i),
                    "precip": hour_value("precipitation_probability", i),
                    "code": hour_value("weather_code", i),
                }
            )
            if len(hourly) >= 24:
                break

        return ModulePayload(
            module=self.name,
            stage={
                "location": self.location_name,
                "current": current,
                "daily": daily,
                "hourly": hourly,
                "sun": {
                    "sunrise": (daily_raw.get("sunrise") or [None])[0],
                    "sunset": (daily_raw.get("sunset") or [None])[0],
                },
            },
            tape=tape,
        )
