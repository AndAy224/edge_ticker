"""Night schedule: scheduled panel dimming and the nightly page reload.

Reads the live config every minute, so admin changes apply without restart.
Dimming prefers DDC/CI (`ddcutil setvcp 10 <level>`); if ddcutil is missing
or fails (e.g. unsupported over USB-C DP-alt), it falls back to broadcasting
a `night` message that the display renders as a software dim overlay.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

BRIGHTNESS_VCP_CODE = "10"


def _in_night_window(night: dict, minute: str) -> bool:
    """Is `minute` ("HH:MM") inside the configured dim window?

    Derived rather than remembered on purpose: a `self._dimmed` flag would reset
    to False on every backend restart, so a takeover during the night after a
    restart would silently skip the brightness boost. "HH:MM" strings compare
    lexicographically in clock order, so this is a plain comparison.
    """
    dim_at = night.get("dim_at")
    wake_at = night.get("wake_at")
    if not dim_at or not wake_at or dim_at == wake_at:
        return False
    if dim_at < wake_at:
        return dim_at <= minute < wake_at
    return minute >= dim_at or minute < wake_at  # window wraps midnight


class NightScheduler:
    def __init__(self, bus, get_config: Callable[[], dict]) -> None:
        self.bus = bus
        self.get_config = get_config
        self._boost: asyncio.Task | None = None

    async def boost(self, seconds: float) -> None:
        """Temporarily undo a hardware dim for a full-screen takeover.

        Only the DDC path needs this: with method=software the display is told
        about night directly and suppresses its own dim overlay while a takeover
        is up. `_tick` is edge-triggered on exact minutes, so the restore has to
        be explicit or the panel would stay bright until the next dim_at.
        """
        night = (self.get_config() or {}).get("night") or {}
        if night.get("method", "ddc") != "ddc":
            return
        if not _in_night_window(night, datetime.now().strftime("%H:%M")):
            return
        if self._boost and not self._boost.done():
            self._boost.cancel()
        day = int(night.get("day_level", 100))
        if not await self._ddcutil(day):
            return  # no ddcutil here — the display's software dim handles it
        log.info("camera takeover: brightness boosted to %d%% for %ss", day, seconds)

        async def restore() -> None:
            try:
                await asyncio.sleep(seconds)
                cfg = (self.get_config() or {}).get("night") or {}
                if _in_night_window(cfg, datetime.now().strftime("%H:%M")):
                    await self._ddcutil(int(cfg.get("dim_level", 10)))
            except asyncio.CancelledError:
                pass

        self._boost = asyncio.create_task(restore(), name="night-boost-restore")

    async def run(self) -> None:
        last_minute = ""
        while True:
            minute = datetime.now().strftime("%H:%M")
            if minute != last_minute:
                last_minute = minute
                try:
                    await self._tick(minute)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("night scheduler: %s", exc)
            await asyncio.sleep(20)

    async def _tick(self, minute: str) -> None:
        night = (self.get_config() or {}).get("night") or {}
        if minute == night.get("dim_at"):
            await self._set_brightness(night, dimming=True)
        if minute == night.get("wake_at"):
            await self._set_brightness(night, dimming=False)
        if minute == night.get("nightly_reload_at"):
            log.info("nightly display reload")
            await self.bus.broadcast({"type": "control", "action": "reload"})

    async def _set_brightness(self, night: dict, dimming: bool) -> None:
        level = int(night.get("dim_level", 10) if dimming else night.get("day_level", 100))
        method = night.get("method", "ddc")
        log.info("night schedule: %s to %d%% via %s", "dim" if dimming else "wake", level, method)
        if method == "ddc" and not await self._ddcutil(level):
            method = "software"
        if method == "software":
            await self.bus.broadcast(
                {"type": "night", "mode": "dim" if dimming else "wake", "level": level}
            )

    @staticmethod
    async def _ddcutil(level: int) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ddcutil",
                "setvcp",
                BRIGHTNESS_VCP_CODE,
                str(level),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        except (FileNotFoundError, OSError):
            return False
