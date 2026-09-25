"""Owns the collector tasks and converges them on the live config."""
from __future__ import annotations

import asyncio
import logging

from .collectors import discover_collectors
from .collectors.base import Collector
from .state import Bus

log = logging.getLogger(__name__)


class CollectorManager:
    """A config save restarts only the collectors whose inputs changed (see
    Collector.config_fingerprint); the rest keep running with their state —
    rate-limit budgets, score diffs, conditional-GET caches — intact."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled  # False in fixture mode: never poll upstream
        self.collectors: list[Collector] = []
        self.skipped: dict[str, str] = {}  # name -> reason it isn't running
        self._tasks: dict[str, asyncio.Task] = {}
        self._fingerprints: dict[str, str] = {}
        # Two saves in quick succession must not interleave stop/start, or one
        # generation of collectors keeps running with nothing holding it.
        self._lock = asyncio.Lock()

    async def apply(self, bus: Bus, config: dict) -> list[str]:
        """Start, stop and restart collectors to match `config`. Returns the
        names that were (re)started."""
        if not self.enabled:
            return []
        async with self._lock:
            found = discover_collectors(config)
            wanted = {c.name: c for c in found.collectors}
            running = {c.name: c for c in self.collectors}
            keep: dict[str, Collector] = {}
            stop: list[str] = []
            for name, collector in running.items():
                task = self._tasks.get(name)
                unchanged = (
                    name in wanted
                    and self._fingerprints.get(name) == type(collector).config_fingerprint(config)
                    and task is not None
                    and not task.done()
                )
                if unchanged:
                    keep[name] = collector
                else:
                    stop.append(name)
            await self._stop(stop)
            for name in stop:
                if name not in wanted:
                    # Disabled or removed: its last payload must not linger on
                    # screen (a tornado warning stayed on the tape until the
                    # process restarted).
                    await bus.remove(name)
            started = [name for name in wanted if name not in keep]
            for name in started:
                collector = wanted[name]
                self._tasks[name] = asyncio.create_task(
                    collector.start(bus), name=f"collector:{name}"
                )
                self._fingerprints[name] = type(collector).config_fingerprint(config)
                keep[name] = collector
            self.collectors = sorted(keep.values(), key=lambda c: c.name)
            self.skipped = found.skipped
            if started or stop:
                log.info(
                    "collectors: started %s, stopped %s, running %s",
                    started, [n for n in stop if n not in wanted], sorted(keep),
                )
            return started

    async def stop(self) -> None:
        async with self._lock:
            await self._stop(list(self._tasks))
            self.collectors = []

    async def _stop(self, names: list[str]) -> None:
        tasks = [self._tasks.pop(n) for n in names if n in self._tasks]
        for name in names:
            self._fingerprints.pop(name, None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> list[dict]:
        rows = []
        for collector in self.collectors:
            row = collector.status()
            task = self._tasks.get(collector.name)
            if task is not None and task.done():
                row["state"] = "dead"  # the loop itself ended; only a restart helps
                row["stuck"] = True
            rows.append(row)
        for name, reason in sorted(self.skipped.items()):
            state, _, detail = reason.partition(": ")
            rows.append({"name": name, "state": state, "detail": detail or None})
        return rows
