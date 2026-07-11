"""Rocket launches — Launch Library 2 (thespacedevs, keyless), detailed mode.

Free tier allows ~15 requests/hour. Budget: idle polls (default 1800s) make
two calls (upcoming detailed + recent results) ≈ 4/hr; inside the live launch
window (T-45m..T+15m) the poll tightens (default 360s) but drops the recent
call ≈ 10/hr. The display renders its own live countdown from `net`.

Florida launches (Cape Canaveral / Kennedy) are flagged: they're visible
from the Tampa Bay area, spectacularly so at night."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

UPCOMING_URL = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/"
PREVIOUS_URL = "https://ll.thespacedevs.com/2.2.0/launch/previous/"

FLORIDA_MARKERS = ("cape canaveral", "kennedy")
KEEP = 8
FINISHED = ("Success", "Failure", "Partial Failure")
LIVE_BEFORE = timedelta(minutes=45)
LIVE_AFTER = timedelta(minutes=15)
DESCRIPTION_MAX = 220


def _florida(location: str) -> bool:
    return any(marker in location.lower() for marker in FLORIDA_MARKERS)


def _parse_net(net: str | None) -> datetime | None:
    if not net:
        return None
    try:
        return datetime.fromisoformat(str(net).replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_live_window(net: str | None, status: str | None, now: datetime) -> bool:
    parsed = _parse_net(net)
    if parsed is None or status in FINISHED:
        return False
    return now - LIVE_AFTER <= parsed <= now + LIVE_BEFORE


def _boosters(entry: dict) -> list[dict]:
    boosters = []
    for stage in (entry.get("rocket") or {}).get("launcher_stage") or []:
        launcher = stage.get("launcher") or {}
        landing = stage.get("landing") or {}
        boosters.append({
            "serial": launcher.get("serial_number"),
            "flight_no": launcher.get("flight_number") or stage.get("launcher_flight_number"),
            "reused": stage.get("reused"),
            "landing_attempt": landing.get("attempt"),
            "landing_type": (landing.get("type") or {}).get("abbrev"),
            "landing_location": (landing.get("location") or {}).get("name"),
        })
    return boosters


class LaunchesCollector(Collector):
    name = "launches"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # Rate limit: never poll faster than ~4 min even if misconfigured.
        self.idle_interval = max(240.0, float(self.module_config.get("poll_seconds", 1800)))
        self.live_interval = max(240.0, float(self.module_config.get("poll_seconds_live", 360)))
        self.interval = self.idle_interval
        self._recent: list[dict] = []  # last-fetched copy, reused during live windows

    async def fetch(self) -> dict:
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                UPCOMING_URL,
                params={"limit": 12, "mode": "detailed", "hide_recent_previous": "true"},
            )
            response.raise_for_status()
            upcoming = response.json()
            live = any(
                _in_live_window(e.get("net"), (e.get("status") or {}).get("abbrev"), now)
                for e in upcoming.get("results", [])
            )
            # Recent results ride along on idle polls only (rate budget).
            if not live or not self._recent:
                try:
                    prev = await client.get(
                        PREVIOUS_URL, params={"limit": 3, "mode": "list"}
                    )
                    prev.raise_for_status()
                    self._recent = [
                        {
                            "name": e.get("name"),
                            "provider": e.get("lsp_name"),
                            "status": (e.get("status") or {}).get("abbrev"),
                            "net": e.get("net"),
                        }
                        for e in prev.json().get("results", [])
                    ]
                except Exception:  # recent strip is decoration — never fail the poll
                    pass
        return upcoming

    def shape(self, raw: dict) -> ModulePayload:
        now = datetime.now(timezone.utc)
        launches = []
        for entry in raw.get("results", []):
            status = entry.get("status") or {}
            mission = entry.get("mission") or {}
            mission_name = mission.get("name") if isinstance(mission, dict) else mission
            description = (mission.get("description") or "") if isinstance(mission, dict) else ""
            if len(description) > DESCRIPTION_MAX:
                description = description[: DESCRIPTION_MAX - 1].rstrip() + "…"
            orbit = (mission.get("orbit") or {}) if isinstance(mission, dict) else {}
            rocket_config = (entry.get("rocket") or {}).get("configuration") or {}
            pad = entry.get("pad") or {}
            pad_name = pad.get("name") if isinstance(pad, dict) else str(pad)
            location = (pad.get("location") or {}).get("name") if isinstance(pad, dict) else ""
            location = location or entry.get("location") or ""
            provider = (entry.get("launch_service_provider") or {}).get("name") or entry.get("lsp_name")
            launches.append({
                "name": entry.get("name"),
                "provider": provider,
                "mission": mission_name,
                "mission_description": description,
                "orbit": orbit.get("abbrev"),
                "orbit_name": orbit.get("name"),
                "net": entry.get("net"),
                "window_start": entry.get("window_start"),
                "window_end": entry.get("window_end"),
                "status": status.get("abbrev"),
                "status_text": status.get("name"),
                "pad": pad_name,
                "location": location,
                "pad_count": pad.get("total_launch_count") if isinstance(pad, dict) else None,
                "image": entry.get("image"),
                "probability": entry.get("probability"),
                "weather_concerns": entry.get("weather_concerns"),
                "programs": [p.get("name") for p in entry.get("program") or [] if p.get("name")],
                "boosters": _boosters(entry),
                "rocket": {
                    "full_name": rocket_config.get("full_name"),
                    "total": rocket_config.get("total_launch_count"),
                    "successes": rocket_config.get("successful_launches"),
                    "streak": rocket_config.get("consecutive_successful_launches"),
                },
                "florida": _florida(str(location)),
                "live": _in_live_window(entry.get("net"), status.get("abbrev"), now),
            })
            if len(launches) >= KEEP:
                break

        live = any(l["live"] for l in launches)
        self.interval = self.live_interval if live else self.idle_interval

        tape: list[TapeItem] = []
        upcoming = [l for l in launches if l.get("net") and l.get("status") not in FINISHED]
        if upcoming:
            nxt = upcoming[0]
            net = _parse_net(nxt["net"])
            when = net.astimezone().strftime("%a %-I:%M %p") if net else ""
            tape.append(TapeItem(
                text=f"{nxt['provider']}: {nxt['name']} — {when}"
                + (" (Canaveral)" if nxt["florida"] else ""),
                accent="alert" if nxt["live"] else "neutral",
            ))

        return ModulePayload(
            module=self.name,
            stage={"launches": launches, "recent": self._recent, "live": live},
            tape=tape,
        )
