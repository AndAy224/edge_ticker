"""Collector contract: fetch() upstream, shape() into a ModulePayload, publish.

Failure policy: a collector never crashes the app. On upstream failure the
last-good payload is kept and re-published with stale=True, then the loop
retries with exponential backoff capped at 5 minutes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..state import Bus, ModulePayload

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 300.0
# Floor under every poll and retry sleep. A cleared number field in the admin
# used to save `poll_seconds: 0`, and a zero interval (with its zero backoff)
# spun the loop flat out against the upstream API.
MIN_INTERVAL_SECONDS = 5.0
# A fetch still running after this long is hung, whatever its own timeouts say.
HUNG_ATTEMPT_SECONDS = 300.0


class Collector(ABC):
    name: str = "base"
    interval: float = 60.0
    # Stretch modules opt out of auto-enable so a fresh install only runs v1 modules.
    enabled_by_default: bool = True
    # Env vars that must be set for this collector to be viable (else skipped).
    required_env: tuple[str, ...] = ()
    # Reads the shared home location from modules.weather — a location edit
    # has to restart this collector too, not only its own module's edits.
    uses_location: bool = False
    # Failure retry pacing. Collectors talking to tightly rate-limited APIs
    # must raise these: fast retries against a 429 burn the hourly budget and
    # lock the collector out permanently (learned from Launch Library 2).
    backoff_start: float = 5.0
    backoff_max: float = MAX_BACKOFF_SECONDS

    def __init__(self, config: dict) -> None:
        self.config = config
        self.module_config: dict = config.get("modules", {}).get(self.name, {})
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.stale = False
        self._last_payload: ModulePayload | None = None
        # Health counters for /api/health. `degraded` is for partial failures a
        # collector survives on its own (one feed of five, the schedule window)
        # — the kind that stayed invisible while the module still reported ok.
        self.degraded: str | None = None
        self.failures_total = 0
        self.consecutive_failures = 0
        self.last_duration_ms: int | None = None
        self._attempt_started: float | None = None  # monotonic; None when idle
        self._attempt_finished: float = time.monotonic()
        self._created = time.monotonic()

    @classmethod
    def config_fingerprint(cls, config: dict) -> str:
        """Everything in `config` this collector's behaviour depends on. A
        config save restarts the collector only when this changes — restarting
        all of them on every save burned Launch Library's hourly budget and
        reset the score diff, so a run during the gap never celebrated."""
        modules = config.get("modules", {}) or {}
        deps: list[Any] = [modules.get(cls.name)]
        if cls.uses_location:
            weather = modules.get("weather") or {}
            deps.append({k: weather.get(k) for k in ("latitude", "longitude", "location_name")})
        return json.dumps(deps, sort_keys=True, default=str)

    async def start(self, bus: Bus) -> None:
        if self._last_payload is None:
            # A restarted collector inherits the previous instance's payload,
            # so its first failure can still mark what's on screen stale.
            self._last_payload = bus.payloads.get(self.name)
        backoff = max(MIN_INTERVAL_SECONDS, min(self.interval, self.backoff_start))
        while True:
            self._attempt_started = time.monotonic()
            try:
                raw = await self.fetch()
                payload = self.shape(raw)
                payload.stale = False
                self.stale = False
                self.last_success = datetime.now(timezone.utc)
                self.last_error = None
                self.consecutive_failures = 0
                self._last_payload = payload
                self._end_attempt()
                await bus.publish(payload)
                backoff = max(MIN_INTERVAL_SECONDS, min(self.interval, self.backoff_start))
                await asyncio.sleep(max(self.interval, MIN_INTERVAL_SECONDS))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._end_attempt()
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.stale = True
                self.failures_total += 1
                self.consecutive_failures += 1
                log.warning("collector %s failed: %s", self.name, self.last_error)
                if self._last_payload is not None and not self._last_payload.stale:
                    stale_payload = self._last_payload.model_copy(update={"stale": True})
                    self._last_payload = stale_payload
                    await bus.publish(stale_payload)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)

    def _end_attempt(self) -> None:
        now = time.monotonic()
        if self._attempt_started is not None:
            self.last_duration_ms = round((now - self._attempt_started) * 1000)
        self._attempt_started = None
        self._attempt_finished = now

    @abstractmethod
    async def fetch(self) -> Any: ...

    @abstractmethod
    def shape(self, raw: Any) -> ModulePayload: ...

    def stuck(self) -> bool:
        """The loop has stopped making attempts: a fetch hung past every
        timeout, or the next attempt is long overdue. Unlike a failing
        upstream, a backend restart actually fixes this."""
        now = time.monotonic()
        if self._attempt_started is not None:
            return now - self._attempt_started > HUNG_ATTEMPT_SECONDS
        longest_sleep = max(self.interval, self.backoff_max, MIN_INTERVAL_SECONDS)
        return now - self._attempt_finished > longest_sleep + 120

    def overdue(self) -> bool:
        """No fresh data for three poll intervals (plus slack)."""
        if self.last_success is None:
            # Since startup, not since the last failed attempt: that resets on
            # every retry, so a collector failing from the start never aged.
            age = time.monotonic() - self._created if self.failures_total else 0.0
        else:
            age = (datetime.now(timezone.utc) - self.last_success).total_seconds()
        return age > 3 * max(self.interval, MIN_INTERVAL_SECONDS) + 60

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": "running",
            "interval": self.interval,
            "stale": self.stale,
            "overdue": self.overdue(),
            "stuck": self.stuck(),
            "degraded": self.degraded,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_error": self.last_error,
            "failures_total": self.failures_total,
            "consecutive_failures": self.consecutive_failures,
            "last_duration_ms": self.last_duration_ms,
        }
