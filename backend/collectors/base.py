"""Collector contract: fetch() upstream, shape() into a ModulePayload, publish.

Failure policy: a collector never crashes the app. On upstream failure the
last-good payload is kept and re-published with stale=True, then the loop
retries with exponential backoff capped at 5 minutes.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..state import Bus, ModulePayload

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 300.0


class Collector(ABC):
    name: str = "base"
    interval: float = 60.0

    def __init__(self, config: dict) -> None:
        self.config = config
        self.module_config: dict = config.get("modules", {}).get(self.name, {})
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.stale = False
        self._last_payload: ModulePayload | None = None

    async def start(self, bus: Bus) -> None:
        backoff = min(self.interval, 5.0)
        while True:
            try:
                raw = await self.fetch()
                payload = self.shape(raw)
                payload.stale = False
                self.stale = False
                self.last_success = datetime.now(timezone.utc)
                self.last_error = None
                self._last_payload = payload
                await bus.publish(payload)
                backoff = min(self.interval, 5.0)
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.stale = True
                log.warning("collector %s failed: %s", self.name, self.last_error)
                if self._last_payload is not None and not self._last_payload.stale:
                    stale_payload = self._last_payload.model_copy(update={"stale": True})
                    self._last_payload = stale_payload
                    await bus.publish(stale_payload)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    @abstractmethod
    async def fetch(self) -> Any: ...

    @abstractmethod
    def shape(self, raw: Any) -> ModulePayload: ...

    def status(self) -> dict:
        return {
            "name": self.name,
            "interval": self.interval,
            "stale": self.stale,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_error": self.last_error,
        }
