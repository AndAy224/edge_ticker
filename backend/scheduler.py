"""Night schedule: scheduled panel dimming and the nightly page reload.

Reads the live config every minute, so admin changes apply without restart.
Dimming prefers DDC/CI (`ddcutil setvcp 10 <level>`); if ddcutil is missing
or fails (e.g. unsupported over USB-C DP-alt), it falls back to broadcasting
a `night` message that the display renders as a software dim overlay.

Brightness is level-triggered: each minute the scheduler works out what the
panel *should* be showing and applies it when that differs from what it last
applied — so a backend restart inside the window, or an admin edit to the
levels, takes effect straight away instead of at the next dim_at/wake_at
minute. A display that connects mid-window gets the current state in its
snapshot (see state()).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

BRIGHTNESS_VCP_CODE = "10"
DDCUTIL_TIMEOUT_SECONDS = 20
# How long the display holds a severe-weather card (weather-alert.ts CARD_MS).
WEATHER_TAKEOVER_SECONDS = 25


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
        # (dimming, level, requested method) last applied; None until the first
        # tick of this process, so startup always reconciles the panel.
        self._applied: tuple[bool, int, str] | None = None
        self.method_used: str | None = None  # "ddc" | "software", as last applied

    def state(self) -> dict:
        """What the display should render right now. `software` is true when
        the dim is the display's job (no working DDC)."""
        night = (self.get_config() or {}).get("night") or {}
        dimming = _in_night_window(night, datetime.now().strftime("%H:%M"))
        level = int(night.get("dim_level", 10) if dimming else night.get("day_level", 100))
        method = self.method_used or night.get("method", "ddc")
        return {"mode": "dim" if dimming else "wake", "level": level, "software": method == "software"}

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
        log.info("takeover: brightness boosted to %d%% for %ss", day, seconds)

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
        watcher = asyncio.create_task(self._watch_takeovers(), name="night-takeovers")
        try:
            await self._run()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _watch_takeovers(self) -> None:
        """A severe-weather takeover needs the panel readable at night too.
        Camera takeovers call boost() themselves; weather alerts are broadcast
        by their collector, so they are picked up off the bus here."""
        queue = self.bus.subscribe(internal=True)
        try:
            while True:
                message = await queue.get()
                if message.get("type") == "weather_alert":
                    await self.boost(WEATHER_TAKEOVER_SECONDS + 5)
        finally:
            self.bus.unsubscribe(queue)

    async def _run(self) -> None:
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
        dimming = _in_night_window(night, minute)
        level = int(night.get("dim_level", 10) if dimming else night.get("day_level", 100))
        wanted = (dimming, level, night.get("method", "ddc"))
        if wanted != self._applied:
            if self._boost and not self._boost.done():
                self._boost.cancel()  # a takeover's restore would undo this
            await self._set_brightness(night, dimming=dimming)
            self._applied = wanted
        if minute == night.get("nightly_reload_at"):
            log.info("nightly display reload")
            await self.bus.broadcast({"type": "control", "action": "reload"})

    async def _set_brightness(self, night: dict, dimming: bool) -> None:
        level = int(night.get("dim_level", 10) if dimming else night.get("day_level", 100))
        method = night.get("method", "ddc")
        log.info("night schedule: %s to %d%% via %s", "dim" if dimming else "wake", level, method)
        if method == "ddc" and not await self._ddcutil(level):
            method = "software"
        self.method_used = method
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
            try:
                return await asyncio.wait_for(proc.wait(), DDCUTIL_TIMEOUT_SECONDS) == 0
            except asyncio.TimeoutError:
                proc.kill()  # a wedged i2c bus must not stall the scheduler
                return False
        except (FileNotFoundError, OSError):
            return False
