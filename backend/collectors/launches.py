"""Rocket launches — Launch Library 2 (thespacedevs, keyless).

Free tier allows ~15 requests/hour, so the poll interval must stay coarse
(default 1800s) — the display renders its own live countdown from `net`.
Florida launches (Cape Canaveral / Kennedy) are flagged: they're visible
from the Tampa Bay area on a clear night."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

UPCOMING_URL = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/"

FLORIDA_MARKERS = ("cape canaveral", "kennedy")
KEEP = 8


def _florida(location: str) -> bool:
    return any(marker in location.lower() for marker in FLORIDA_MARKERS)


class LaunchesCollector(Collector):
    name = "launches"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # Rate limit: never poll faster than ~4 min even if misconfigured.
        self.interval = max(240.0, float(self.module_config.get("poll_seconds", 1800)))

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                UPCOMING_URL,
                params={"limit": 12, "mode": "list", "hide_recent_previous": "true"},
            )
        response.raise_for_status()
        return response.json()

    def shape(self, raw: dict) -> ModulePayload:
        launches = []
        for entry in raw.get("results", []):
            status = entry.get("status") or {}
            location = entry.get("location") or ""
            launches.append({
                "name": entry.get("name"),
                "provider": entry.get("lsp_name"),
                "mission": entry.get("mission"),
                "mission_type": entry.get("mission_type"),
                "net": entry.get("net"),
                "window_start": entry.get("window_start"),
                "window_end": entry.get("window_end"),
                "status": status.get("abbrev"),
                "status_text": status.get("name"),
                "pad": entry.get("pad"),
                "location": location,
                "image": entry.get("image"),
                "florida": _florida(location),
            })
            if len(launches) >= KEEP:
                break

        tape: list[TapeItem] = []
        upcoming = [
            l for l in launches
            if l.get("net") and l.get("status") not in ("Success", "Failure")
        ]
        if upcoming:
            nxt = upcoming[0]
            net = datetime.fromisoformat(str(nxt["net"]).replace("Z", "+00:00"))
            hours = (net - datetime.now(timezone.utc)).total_seconds() / 3600
            when = net.astimezone().strftime("%a %-I:%M %p")
            tape.append(TapeItem(
                text=f"{nxt['provider']}: {nxt['name']} — {when}"
                + (" (Canaveral)" if nxt["florida"] else ""),
                accent="alert" if 0 <= hours <= 1 else "neutral",
            ))

        return ModulePayload(module=self.name, stage={"launches": launches}, tape=tape)
