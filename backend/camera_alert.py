"""Source-agnostic camera-alert takeover events.

Any backend producer — the HA bridge, a future motion collector, a doorbell
webhook — calls `fire()` with a title, a severity and a list of camera
entity_ids. This module mints the opaque proxy tokens, applies a per-key
cooldown, and puts one small `camera_alert` message on the bus. The display
never sees an entity_id or an upstream URL, and image bytes never touch the
WebSocket (`Bus.broadcast` drops for slow clients — frames go over HTTP).
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_DURATION = 30
MIN_DURATION, MAX_DURATION = 5, 120
# A door edge is one event, so a long guard is safe here — unlike the sports
# celebration cooldown, where a blanket long value silently dropped legitimate
# runs (see SPORT_COOLDOWN_SECONDS in collectors/sports.py). Kept overridable
# so a doorbell or motion consumer can go much shorter.
DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_CAMERAS = 4

SEVERITIES = {"info", "alert", "critical"}
TRANSPORTS = {"stream", "snapshot"}


def camera_token(entity_id: str) -> str:
    """Stable opaque id for a camera entity.

    The allowlist (`allowed_cameras`) is the security boundary; this only keeps
    entity_ids out of the display DOM and out of admin-visible payloads.
    """
    return hashlib.sha256(entity_id.encode()).hexdigest()[:16]


def _camera_entity(item) -> str | None:
    """Accept either a bare entity_id or a {entity, label} dict.

    Config is authored by hand and by the admin GUI, and `PUT /api/config`
    validates nothing — so tolerate both shapes and reject everything else.
    """
    eid = item.get("entity") if isinstance(item, dict) else item
    if isinstance(eid, str) and eid.startswith("camera."):
        return eid
    return None


def allowed_cameras(config: dict) -> dict[str, str]:
    """token -> entity_id, derived from LIVE config.

    Anything not explicitly selected by the user is not proxyable. This is what
    keeps /api/cameras from being an open proxy into Home Assistant.
    """
    ha = (config or {}).get("ha") or {}
    ids: set[str] = set()
    for item in ha.get("cameras") or []:
        eid = _camera_entity(item)
        if eid:
            ids.add(eid)
    for alert in ha.get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        for item in alert.get("cameras") or []:
            eid = _camera_entity(item)
            if eid:
                ids.add(eid)
    return {camera_token(e): e for e in ids}


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class CameraAlertHub:
    """Mints and broadcasts `camera_alert` events, and debounces re-fires."""

    def __init__(
        self,
        bus,
        get_config: Callable[[], dict],
        friendly_name: Callable[[str], str] | None = None,
        scheduler=None,
    ) -> None:
        self.bus = bus
        self.get_config = get_config
        self.friendly_name = friendly_name or (lambda eid: eid)
        self.scheduler = scheduler  # optional: DDC brightness boost on the appliance
        self._fired_at: dict[str, float] = {}

    # -- producer surface ---------------------------------------------------

    async def fire(
        self,
        *,
        key: str,
        source: str,
        title: str,
        cameras: list,
        subtitle: str | None = None,
        severity: str = "alert",
        duration_seconds=DEFAULT_DURATION,
        wake: bool = True,
        transport: str = "stream",
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> dict | None:
        """Broadcast a takeover. Returns None (never raises) if suppressed."""
        now = time.monotonic()
        if cooldown_seconds > 0:
            last = self._fired_at.get(key)
            if last is not None and now - last < cooldown_seconds:
                log.info("camera alert %s suppressed by cooldown", key)
                return None
        self._fired_at[key] = now

        tiles = []
        for item in cameras or []:
            eid = _camera_entity(item)
            if not eid:
                continue
            label = item.get("label") if isinstance(item, dict) else None
            tiles.append({"id": camera_token(eid), "label": label or self.friendly_name(eid)})
            if len(tiles) >= MAX_CAMERAS:
                break
        if not tiles:
            log.info("camera alert %s has no usable cameras — not firing", key)
            return None

        event = {
            "id": uuid.uuid4().hex[:12],
            "key": key,
            "source": source,
            "title": str(title or "Camera alert"),
            "subtitle": subtitle,
            "severity": severity if severity in SEVERITIES else "alert",
            "transport": transport if transport in TRANSPORTS else "stream",
            "duration_seconds": _clamp_int(
                duration_seconds, DEFAULT_DURATION, MIN_DURATION, MAX_DURATION
            ),
            "wake": bool(wake),
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "cameras": tiles,
        }
        await self.bus.broadcast({"type": "camera_alert", "event": event})
        log.info(
            "camera alert: %s (%d cameras, %ds)",
            event["title"],
            len(tiles),
            event["duration_seconds"],
        )
        if self.scheduler is not None:
            # Hardware dim is a backend concern: with method=ddc the display is
            # never told about night at all, so only the scheduler can undo it.
            try:
                await self.scheduler.boost(event["duration_seconds"] + 5)
            except Exception as exc:  # a brightness failure must not kill the alert
                log.warning("camera alert brightness boost: %s", exc)
        return event

    # -- Home Assistant edge detection --------------------------------------

    async def on_ha_state_change(
        self, entity_id: str, old: str | None, new: str | None
    ) -> None:
        """Fire on the entry edge of a configured takeover alert.

        Leaving the alert state stays a toast — only the attention-worthy
        direction takes the panel over.
        """
        if old is None or new is None or old == new:
            return  # entity added, or an attribute-only change
        ha = (self.get_config() or {}).get("ha") or {}
        for alert in ha.get("alerts") or []:
            if not isinstance(alert, dict) or alert.get("entity") != entity_id:
                continue
            if not alert.get("takeover") or new != alert.get("state"):
                return
            name = self.friendly_name(entity_id)
            await self.fire(
                key=f"ha:{entity_id}",
                source="home_assistant",
                title=alert.get("takeover_title") or alert.get("text") or name,
                subtitle=name,
                cameras=alert.get("cameras") or [],
                severity=alert.get("severity") or "alert",
                duration_seconds=alert.get("duration_seconds") or DEFAULT_DURATION,
                transport=alert.get("transport") or "stream",
                cooldown_seconds=_cooldown(alert),
            )
            return


def _cooldown(alert: dict) -> float:
    try:
        value = float(alert.get("cooldown_seconds"))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SECONDS
    return max(0.0, value)


def first_takeover_alert(config: dict) -> dict | None:
    """The first alert configured for a takeover — used by the test action."""
    ha = (config or {}).get("ha") or {}
    for alert in ha.get("alerts") or []:
        if isinstance(alert, dict) and alert.get("takeover") and alert.get("cameras"):
            return alert
    return None


__all__ = [
    "CameraAlertHub",
    "allowed_cameras",
    "camera_token",
    "first_takeover_alert",
    "DEFAULT_DURATION",
    "MAX_CAMERAS",
]
