"""Rocket launches — Launch Library 2 (thespacedevs, keyless), detailed mode.

Free tier allows ~15 requests/hour PER IP — dev and prod share it, and a 429
lockout self-perpetuates if retried fast (the default collector backoff would
burn the whole budget on retries). Budget: idle polls (default 1800s) make
one call, plus the recent-results call every 4th poll ≈ 2.5/hr; inside the
live launch window (T-45m..T+15m) the poll tightens (default 360s, single
call) ≈ 10/hr; failures retry no sooner than 15 minutes. An idle poll that
would sleep past the opening of a live window wakes for it instead — one
earlier poll per launch, never sooner than the live cadence. The display
renders its own live countdown from `net`.

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


RECENT_EVERY_N_POLLS = 4
# Rate limit: never poll faster than ~4 min even if misconfigured.
MIN_POLL_SECONDS = 240.0
WAKE_MARGIN_SECONDS = 5.0  # past the window's opening, so the wake-up poll is inside it


class LaunchesCollector(Collector):
    name = "launches"
    # 429 recovery must be slow: retrying at the default 5s..300s cadence
    # spends 12+ req/hr on failures alone and never gets back under the limit.
    backoff_start = 900.0
    backoff_max = 3600.0

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.idle_interval = max(MIN_POLL_SECONDS, float(self.module_config.get("poll_seconds", 1800)))
        self.live_interval = max(MIN_POLL_SECONDS, float(self.module_config.get("poll_seconds_live", 360)))
        self.interval = self.idle_interval
        self._woken_for: set[str] = set()  # launch ids already given their early poll
        self._recent: list[dict] = []  # last-fetched copy, reused between refreshes
        self._polls_since_recent = 0
        self._recent_tried = False

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
            # Recent results ride along on every Nth idle poll (rate budget),
            # plus once at startup. Not "whenever none are cached": a previous-
            # results call that kept failing or coming back empty made every
            # poll two calls — ~20/hr in the live window, over the ~15/hr limit.
            self._polls_since_recent += 1
            due = not live and self._polls_since_recent >= RECENT_EVERY_N_POLLS
            if due or not self._recent_tried:
                self._recent_tried = True
                self._polls_since_recent = 0
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

    def _idle_interval(self, raw: dict, now: datetime) -> float:
        """The idle cadence, cut short when a live window opens before the next
        idle poll: the T-0 board (and its auto-pin) used to start up to an idle
        interval (30 min) into the 45-minute run-up.

        Budget: one early poll per launch, remembered by id — a NET that keeps
        slipping would otherwise pull a poll forward at every slip — and never
        sooner than the live cadence, so it costs no more than the live window
        it leads into."""
        opening: tuple[float, str] | None = None
        listed: set[str] = set()
        for entry in raw.get("results", []):
            net = _parse_net(entry.get("net"))
            launch_id = str(entry.get("id") or entry.get("name") or "")
            listed.add(launch_id)
            if net is None or (entry.get("status") or {}).get("abbrev") in FINISHED:
                continue
            if not launch_id or launch_id in self._woken_for:
                continue
            ahead = (net - LIVE_BEFORE - now).total_seconds()
            if 0 < ahead and (opening is None or ahead < opening[0]):
                opening = (ahead, launch_id)
        self._woken_for &= listed  # flown launches leave the list
        if opening is None:
            return self.idle_interval
        wake = max(self.live_interval, opening[0] + WAKE_MARGIN_SECONDS)
        if wake >= self.idle_interval:
            return self.idle_interval
        self._woken_for.add(opening[1])
        return wake

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
                "starship": "starship" in str(entry.get("name") or "").lower(),
                "live": _in_live_window(entry.get("net"), status.get("abbrev"), now),
            })
            if len(launches) >= KEEP:
                break

        live = any(l["live"] for l in launches)
        self.interval = self.live_interval if live else self._idle_interval(raw, now)

        tape: list[TapeItem] = []
        upcoming = [l for l in launches if l.get("net") and l.get("status") not in FINISHED]
        if upcoming:
            # Starship flight day owns the tape slot (backend and kiosk share a clock).
            today = datetime.now().astimezone().date()
            ship = next(
                (
                    l for l in upcoming
                    if l["starship"]
                    and (net := _parse_net(l["net"]))
                    and net.astimezone().date() == today
                ),
                None,
            )
            nxt = ship or upcoming[0]
            net = _parse_net(nxt["net"])
            when = net.astimezone().strftime("%a %-I:%M %p") if net else ""
            tape.append(TapeItem(
                text=f"{nxt['provider']}: {nxt['name']} — {when}"
                + (" (Canaveral)" if nxt["florida"] else ""),
                accent="alert" if (nxt["live"] or ship is not None) else "neutral",
            ))

        return ModulePayload(
            module=self.name,
            stage={"launches": launches, "recent": self._recent, "live": live},
            tape=tape,
        )
