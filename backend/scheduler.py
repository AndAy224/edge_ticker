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


class NightScheduler:
    def __init__(self, bus, get_config: Callable[[], dict]) -> None:
        self.bus = bus
        self.get_config = get_config

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
