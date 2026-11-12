"""Night schedule: scheduled panel dimming and the nightly page reload.

Reads the live config every minute, so admin changes apply without restart.
Dimming prefers DDC/CI (`ddcutil setvcp 10 <level>`); if ddcutil is missing
or fails (e.g. unsupported over USB-C DP-alt), it falls back to broadcasting
a `night` message that the display renders as a software dim overlay.

Brightness is level-triggered: each minute the scheduler works out what the
panel *should* be showing and applies it when that differs from what it last
applied — so a backend restart inside the window, or an admin edit to the
levels, takes effect straight away instead of at the next dim_at/wake_at
minute. While DDC is driving the panel the backlight itself is checked too
(`_hw`, what ddcutil last confirmed), so a failed takeover boost or restore
is retried the next minute. A display that connects mid-window gets the
current state in its snapshot (see state()).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Callable

from .tasks import spawn

log = logging.getLogger(__name__)

BRIGHTNESS_VCP_CODE = "10"
DDCUTIL_TIMEOUT_SECONDS = 20
# How long the display holds a severe-weather card (weather-alert.ts CARD_MS).
WEATHER_TAKEOVER_SECONDS = 25


def _level(night: dict, dimming: bool) -> int:
    """The brightness for this side of the window (a null level falls back)."""
    key, default = ("dim_level", 10) if dimming else ("day_level", 100)
    value = night.get(key)
    return int(default if value is None else value)


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


def _now_minute() -> str:
    return datetime.now().strftime("%H:%M")


class NightScheduler:
    def __init__(self, bus, get_config: Callable[[], dict]) -> None:
        self.bus = bus
        self.get_config = get_config
        # The takeover boost: one worker holds the panel bright until
        # `_boost_until` (loop time), which a takeover arriving meanwhile
        # extends, then hands the backlight back to the schedule.
        self._boost: asyncio.Task | None = None
        self._boost_until = 0.0
        self.watcher: asyncio.Task | None = None  # weather takeovers (for /api/health)
        # (dimming, level, requested method) last applied; None until the first
        # tick of this process, so startup always reconciles the panel.
        self._applied: tuple[bool, int, str] | None = None
        self.method_used: str | None = None  # "ddc" | "software", as last applied
        # The backlight level ddcutil last confirmed; None = unknown (nothing
        # written yet, or the last write failed, timed out or was cancelled).
        self._hw: int | None = None
        # The last level a write confirmed, kept through later failures: where
        # the backlight most likely still is.
        self._hw_last: int | None = None
        # One ddcutil at a time: a boost starting while the tick writes the
        # dim would otherwise race it on the i2c bus.
        self._ddc_lock = asyncio.Lock()

    def _night(self) -> dict:
        return (self.get_config() or {}).get("night") or {}

    def state(self) -> dict:
        """What the display should render right now. `software` is true when
        the dim is the display's job (no working DDC)."""
        night = self._night()
        dimming = _in_night_window(night, _now_minute())
        level = _level(night, dimming)
        method = self.method_used or night.get("method", "ddc")
        return {"mode": "dim" if dimming else "wake", "level": level, "software": method == "software"}

    def _stranded(self, level: int) -> bool:
        """DDC has stopped answering with the backlight below `level`.

        The software fallback only darkens — an overlay over whatever the
        backlight gives — so it covers a failed dim, not a failed wake: one
        ddcutil failure at wake_at used to leave the panel at the night level
        until the next evening, with the display told all was well."""
        return (
            self._night().get("method", "ddc") == "ddc"
            and self.method_used == "software"
            and self._hw_last is not None
            and self._hw_last < level
        )

    def problem(self) -> str | None:
        """For /api/health: DDC is configured and not doing the job."""
        night = self._night()
        if night.get("method", "ddc") != "ddc" or self.method_used != "software":
            return None
        level = _level(night, _in_night_window(night, _now_minute()))
        if self._stranded(level):
            return (
                f"night: ddcutil is failing with the backlight last set to {self._hw_last}% "
                f"(wanted {level}%) — retrying every minute"
            )
        return "night: ddcutil is failing — the display dims in software instead"

    def boost(self, seconds: float) -> None:
        """Lift a hardware dim for a full-screen takeover; returns at once.

        Only the DDC path needs this: with method=software the display is told
        about night directly and suppresses its own dim overlay while a takeover
        is up — likewise when DDC failed at the dim and the display is drawing
        it (method_used "software"). The minute tick leaves the backlight alone
        while a boost holds (unless the schedule itself changes, which ends the
        boost), so putting the dim back is the boost worker's job.

        ddcutil runs in the worker, never in the caller: camera takeovers call
        this from inside the HA bridge's reader, and waiting there on ddcutil
        (up to 20 s) held up the WebRTC answers the takeover's tiles needed.
        """
        night = self._night()
        if night.get("method", "ddc") != "ddc" or self.method_used == "software":
            return
        if not _in_night_window(night, _now_minute()):
            return
        loop = asyncio.get_running_loop()
        # One deadline, extended by every takeover: a second takeover used to
        # cancel the first's restore and, if its own ddcutil call then failed,
        # schedule none — the panel stayed at full brightness until morning.
        # Two boosts in flight at once each left a restore behind, and the
        # orphan re-dimmed the panel in the middle of the later takeover.
        self._boost_until = max(self._boost_until, loop.time() + seconds)
        if not self._boosting():
            self._boost = spawn(self._hold_boost(), "night-boost")

    def _boosting(self) -> bool:
        return self._boost is not None and not self._boost.done()

    async def _hold_boost(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            night = self._night()
            holding = self._boost_until > loop.time()
            if holding:
                target = _level(night, dimming=False)
            else:  # hand the backlight back to the schedule
                target = _level(
                    night,
                    dimming=night.get("method", "ddc") == "ddc"
                    and _in_night_window(night, _now_minute()),
                )
            if self._hw != target:
                if await self._write(target):
                    log.info("takeover: brightness %s to %d%%", "boosted" if holding else "restored", target)
                elif holding:
                    log.warning("takeover: brightness boost to %d%% failed", target)
                else:
                    log.warning("takeover: restoring brightness to %d%% failed — the next tick retries", target)
            remaining = self._boost_until - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)  # (re-)boosted: hold, then go round again
            elif not holding:
                return

    async def _end_boost(self) -> None:
        """Stop a boost before the schedule writes the backlight itself."""
        self._boost_until = 0.0
        task = self._boost
        if task is not None and not task.done():
            task.cancel()
            # Awaited so its ddcutil child is dead before the next one starts.
            await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        self.watcher = watcher = spawn(self._watch_takeovers(), "night-takeovers")
        try:
            await self._run()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await self._end_boost()

    async def _watch_takeovers(self) -> None:
        """A severe-weather takeover needs the panel readable at night too.
        Camera takeovers call boost() themselves; weather alerts are broadcast
        by their collector, so they are picked up off the bus here."""
        queue = self.bus.subscribe(internal=True)
        try:
            while True:
                message = await queue.get()
                try:
                    if message.get("type") == "weather_alert":
                        self.boost(WEATHER_TAKEOVER_SECONDS + 5)
                except Exception as exc:
                    # Unguarded, one bad boost (a malformed config, say) ended
                    # this loop, and every later weather alert went unboosted.
                    log.warning("weather takeover boost: %s", exc)
        finally:
            self.bus.unsubscribe(queue)

    async def _run(self) -> None:
        last_minute = ""
        while True:
            minute = _now_minute()
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
        night = self._night()
        dimming = _in_night_window(night, minute)
        level = _level(night, dimming)
        wanted = (dimming, level, night.get("method", "ddc"))
        # Level-triggered on the backlight too while DDC drives it: a failed
        # boost restore used to leave the panel at full brightness until
        # morning, since `_applied` still said "dim". Retried once a minute
        # only while DDC is believed to work — when the retry fails as well,
        # the software dim takes over (method_used) and the retries stop.
        drifted = (
            wanted[2] == "ddc"
            and self.method_used == "ddc"
            and not self._boosting()
            and self._hw != level
        )
        # ... except where the software dim can't take over: a backlight left
        # below the wanted level is retried until a write lands.
        if wanted != self._applied or drifted or self._stranded(level):
            await self._end_boost()  # a takeover's restore would undo this
            await self._set_brightness(night, dimming=dimming)
            self._applied = wanted
        if minute == night.get("nightly_reload_at"):
            log.info("nightly display reload")
            await self.bus.broadcast({"type": "control", "action": "reload"})

    async def _set_brightness(self, night: dict, dimming: bool) -> None:
        level = _level(night, dimming)
        method = night.get("method", "ddc")
        log.info("night schedule: %s to %d%% via %s", "dim" if dimming else "wake", level, method)
        if method == "ddc":
            if not await self._write(level):
                method = "software"
        elif self.method_used == "ddc" and self._hw != _level(night, dimming=False):
            # Leaving DDC with the backlight dimmed: software mode never runs
            # ddcutil, so the morning wake only lifted the overlay and the
            # backlight stayed at dim_level for good. One write, then the
            # overlay does the dimming.
            await self._write(_level(night, dimming=False))
        self.method_used = method
        # Sent whichever way it went: a display left dimmed in software by an
        # earlier ddcutil failure has to hear that DDC is handling it now.
        await self.bus.broadcast({
            "type": "night",
            "mode": "dim" if dimming else "wake",
            "level": level,
            "software": method == "software",
        })

    async def _write(self, level: int) -> bool:
        """Set the backlight over DDC, keeping `_hw` to what is known to hold."""
        async with self._ddc_lock:
            self._hw = None  # a write that times out or is cancelled may have landed or not
            if await self._ddcutil(level):
                self._hw = self._hw_last = level
                return True
            return False

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
        except (FileNotFoundError, OSError):
            return False
        try:
            return await asyncio.wait_for(proc.wait(), DDCUTIL_TIMEOUT_SECONDS) == 0
        except asyncio.TimeoutError:
            proc.kill()  # a wedged i2c bus must not stall the scheduler
            return False
        except asyncio.CancelledError:
            # Only the timeout path used to kill it: a cancelled boost left its
            # ddcutil running on the bus behind the write that replaced it.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise
