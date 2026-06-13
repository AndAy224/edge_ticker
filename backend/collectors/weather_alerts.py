"""Severe weather alerts — National Weather Service (keyless, US only).

Separate from the weather collector on purpose: alerts need a much tighter
poll (a Tornado Warning can't wait out a 15-minute forecast cycle) and NWS
has routine 5xx blips that shouldn't mark the forecast stale.

Active alerts become high-priority tape items; Extreme/Severe *warnings*
additionally broadcast a `weather_alert` message that the display renders as
a full-screen takeover (parallel to the sports `sport_event` pipeline).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from ..state import Bus, ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

ALERTS_URL = "https://api.weather.gov/alerts/active"
# NWS rejects requests without a User-Agent (and asks that it identify the app).
HEADERS = {
    "User-Agent": "edge-ticker/1.0 (kiosk appliance)",
    "Accept": "application/geo+json",
}

MAX_TAPE_ALERTS = 4
OVERLAY_SEVERITIES = ("Extreme", "Severe")
# NWS issues an Update as a brand-new alert id; without a per-event cooldown a
# long-running warning would re-take the screen on every update.
OVERLAY_COOLDOWN_SECONDS = 600.0

# Overlay dedup state is module-level on purpose: PUT /api/config restarts all
# collectors, and instance state would replay the full-screen takeover every
# time anyone saves config during an active warning. (A genuinely active alert
# does fire once at process start — desired for a safety overlay.)
_fired_ids: dict[str, float] = {}  # alert id -> monotonic time fired
_event_fired_at: dict[str, float] = {}  # event name -> monotonic time fired
_FIRED_TTL_SECONDS = 24 * 3600.0

# Canned event for POST /api/control {"action": "weather_alert_test"} — unlike
# celebrate_test there may be no live alert to replay, so this is hardcoded.
TEST_ALERT = {
    "id": "test-alert",
    "event": "Tornado Warning",
    "severity": "Extreme",
    "urgency": "Immediate",
    "headline": "Tornado Warning issued for Pinellas County until 6:15 PM EDT",
    "area": "Pinellas County, FL",
    "onset": None,
    "ends": None,
    "instruction": (
        "TAKE COVER NOW! Move to a basement or an interior room on the lowest "
        "floor of a sturdy building. Avoid windows."
    ),
}


def _short_time(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.strftime("%I:%M %p").lstrip("0")


class WeatherAlertsCollector(Collector):
    name = "weather_alerts"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 180))
        self.overlay_enabled = self.module_config.get("overlay", True) is not False
        # Reuse the weather module's location so the admin location card stays
        # the single source of truth (same as adsb and astro).
        weather_cfg = config.get("modules", {}).get("weather", {})
        # api.weather.gov rejects `point` params with more than 4 decimals.
        self.latitude = round(float(weather_cfg.get("latitude", 27.9659)), 4)
        self.longitude = round(float(weather_cfg.get("longitude", -82.8001)), 4)
        self.location_name = weather_cfg.get("location_name", "Clearwater, FL")
        self._bus: Bus | None = None
        self._last_alerts: list[dict] = []

    async def start(self, bus: Bus) -> None:
        self._bus = bus  # kept for out-of-band weather_alert broadcasts
        await super().start(bus)

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            response = await client.get(
                ALERTS_URL,
                params={"point": f"{self.latitude},{self.longitude}"},
            )
        response.raise_for_status()
        alerts = [
            parsed
            for feature in response.json().get("features", [])
            if (parsed := self._parse(feature)) is not None
        ]
        self._last_alerts = alerts
        await self._maybe_overlay(alerts)
        return alerts

    @staticmethod
    def _parse(feature: dict) -> dict | None:
        p = feature.get("properties") or {}
        # Drop test/exercise/draft messages and cancellations.
        if p.get("status") != "Actual" or p.get("messageType") not in ("Alert", "Update"):
            return None
        return {
            "id": p.get("id"),
            "event": p.get("event") or "Weather alert",
            "severity": p.get("severity") or "Unknown",
            "urgency": p.get("urgency"),
            "headline": p.get("headline"),
            "area": p.get("areaDesc"),
            "onset": p.get("onset"),
            "ends": p.get("ends") or p.get("expires"),
            "instruction": (p.get("instruction") or "")[:280] or None,
        }

    def shape(self, raw: list[dict]) -> ModulePayload:
        tape = []
        for a in raw[:MAX_TAPE_ALERTS]:
            until = _short_time(a.get("ends"))
            text = f"{a['event']} — until {until}" if until else a["event"]
            # Above HA alerts (2) and followed-game items (1).
            tape.append(TapeItem(text=text, accent="alert", priority=3, icon="warning"))
        return ModulePayload(
            module=self.name,
            stage={"alerts": raw, "location": self.location_name},
            tape=tape,
        )

    async def _maybe_overlay(self, alerts: list[dict]) -> None:
        if self._bus is None or not self.overlay_enabled:
            return
        now = time.monotonic()
        for stale_id in [i for i, t in _fired_ids.items() if now - t > _FIRED_TTL_SECONDS]:
            del _fired_ids[stale_id]
        for a in alerts:
            # Warnings only: NWS marks e.g. Severe Thunderstorm *Watch* as
            # "Severe" too, and a watch isn't worth taking over the screen.
            if a["severity"] not in OVERLAY_SEVERITIES:
                continue
            if "warning" not in a["event"].lower():
                continue
            if a["id"] in _fired_ids:
                continue
            if now - _event_fired_at.get(a["event"], -OVERLAY_COOLDOWN_SECONDS) < OVERLAY_COOLDOWN_SECONDS:
                continue
            _fired_ids[a["id"]] = now
            _event_fired_at[a["event"]] = now
            log.info("weather alert overlay: %s (%s)", a["event"], a["severity"])
            await self._bus.broadcast({"type": "weather_alert", "alert": a})

    def status(self) -> dict:
        return super().status() | {"active_alerts": len(self._last_alerts)}
