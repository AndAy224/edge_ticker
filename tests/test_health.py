"""backend/main.py: /api/health's kiosk count and dead background tasks, and
the loggers that must stay quiet."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from backend import main
from backend import ws as ws_module
from backend.scheduler import NightScheduler
from backend.state import Bus
from helpers import FakeBus


def test_websockets_never_logs_handshakes():
    # websockets logs the request line at DEBUG, and Finnhub's socket carries
    # its key in the URL: LOG_LEVEL=DEBUG put it in the journal.
    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.DEBUG)  # what LOG_LEVEL=DEBUG does
    try:
        assert not logging.getLogger("websockets.client").isEnabledFor(logging.DEBUG)
    finally:
        root.setLevel(saved)


async def test_health_lists_kiosk_clients_and_dead_background_tasks(monkeypatch):
    monkeypatch.setattr(ws_module, "display_clients", 2)
    monkeypatch.setattr(ws_module, "kiosk_clients", 1)
    monkeypatch.setattr(ws_module, "_kiosks", set())

    async def crash():
        raise RuntimeError("boom")

    async def forever():
        await asyncio.Event().wait()

    dead = asyncio.create_task(crash(), name="ha-bridge")
    alive = asyncio.create_task(forever(), name="night-scheduler")
    await asyncio.wait({dead})
    scheduler = NightScheduler(FakeBus(), lambda: {})
    state = SimpleNamespace(
        bus=Bus(),
        ha=SimpleNamespace(status="connected"),
        manager=SimpleNamespace(status=lambda: []),
        scheduler=scheduler,
        background=[dead, alive],
    )
    body = main.health(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert (body["display_clients"], body["kiosk_clients"]) == (2, 1)
    assert body["problems"] == ["ha-bridge: background task has died (RuntimeError('boom'))"]
    # ... and by name, for the watchdog: a restart is what brings a task back
    assert body["dead_tasks"] == ["ha-bridge"]
    assert body["kiosk"] is None  # no display socket from this machine to judge
    alive.cancel()
    await asyncio.gather(alive, return_exceptions=True)


async def test_health_reports_whether_the_kiosk_is_still_drawing(monkeypatch):
    # A connected socket is not a working panel; the watchdog reads these ages.
    now = [500.0]
    monkeypatch.setattr(ws_module, "_now", lambda: now[0])
    kiosk = ws_module.Liveness()
    monkeypatch.setattr(ws_module, "_kiosks", {kiosk})
    kiosk.ping({"type": "ping", "frames": 9})
    now[0] += 75
    state = SimpleNamespace(
        bus=Bus(),
        ha=SimpleNamespace(status="connected"),
        manager=SimpleNamespace(status=lambda: []),
        scheduler=NightScheduler(FakeBus(), lambda: {}),
    )
    body = main.health(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert body["kiosk"] == {"connected_for": 75.0, "ping_age": 75.0, "frame_age": 75.0, "blanked": False}
    assert body["dead_tasks"] == []
