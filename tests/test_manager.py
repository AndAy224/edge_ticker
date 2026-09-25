"""backend/manager.py: CollectorManager converges running collectors on config,
restarting only those whose config fingerprint changed. Real collector classes
with fetch/shape stubbed out — nothing touches the network."""
from __future__ import annotations

import asyncio
import copy

import pytest

from backend.collectors import collector_classes
from backend.collectors.news import NewsCollector
from backend.collectors.weather import WeatherCollector
from backend.collectors.weather_radar import WeatherRadarCollector
from backend.manager import CollectorManager
from backend.state import Bus, ModulePayload
from helpers import until

RUNNING = ("news", "weather", "weather_radar")


@pytest.fixture
def config(defaults_config) -> dict:
    """Defaults with everything but news/weather/weather_radar switched off
    (including any module added later that isn't in defaults.yaml)."""
    modules = defaults_config["modules"]
    for cls in collector_classes():
        modules.setdefault(cls.name, {})
    for name, module in modules.items():
        module["enabled"] = name in RUNNING
    return defaults_config


@pytest.fixture
def stubbed(monkeypatch) -> dict[str, int]:
    """Stub fetch/shape; count fetches per module."""
    fetches: dict[str, int] = {}

    async def fetch(self):
        fetches[self.name] = fetches.get(self.name, 0) + 1
        return fetches[self.name]

    def shape(self, raw):
        return ModulePayload(module=self.name, stage={"poll": raw, "instance": id(self)})

    for cls in (NewsCollector, WeatherCollector, WeatherRadarCollector):
        monkeypatch.setattr(cls, "fetch", fetch)
        monkeypatch.setattr(cls, "shape", shape)
    return fetches


@pytest.fixture
async def manager(stubbed):
    manager = CollectorManager()
    yield manager
    await manager.stop()


def by_name(manager: CollectorManager) -> dict:
    return {c.name: c for c in manager.collectors}


async def started(manager, bus, config) -> list[str]:
    names = await manager.apply(bus, config)
    await until(lambda: all(n in bus.payloads for n in by_name(manager)))
    return names


async def test_first_apply_starts_enabled_collectors(manager, config):
    bus = Bus()
    names = await started(manager, bus, config)
    assert sorted(names) == list(RUNNING)
    assert [c.name for c in manager.collectors] == list(RUNNING)  # sorted
    assert set(bus.payloads) == set(RUNNING)
    assert manager.skipped["adsb"] == "disabled"
    assert all(not t.done() for t in manager._tasks.values())


async def test_reapplying_the_same_config_restarts_nothing(manager, config, stubbed):
    bus = Bus()
    await started(manager, bus, config)
    before = by_name(manager)
    assert await manager.apply(bus, copy.deepcopy(config)) == []
    after = by_name(manager)
    assert all(after[n] is before[n] for n in RUNNING)
    assert stubbed == {n: 1 for n in RUNNING}  # no extra polls


async def test_unrelated_edit_restarts_nothing(manager, config):
    bus = Bus()
    await started(manager, bus, config)
    config = copy.deepcopy(config)
    config["appearance"]["theme"] = "frost"
    config["rotation"]["order"] = ["news"]
    config["modules"]["sports"]["days_back"] = 1  # sports is disabled
    assert await manager.apply(bus, config) == []


async def test_own_module_edit_restarts_only_that_collector(manager, config):
    bus = Bus()
    await started(manager, bus, config)
    before = by_name(manager)
    config = copy.deepcopy(config)
    config["modules"]["news"]["keep"] = 10
    assert await manager.apply(bus, config) == ["news"]
    after = by_name(manager)
    assert after["news"] is not before["news"] and after["news"].keep == 10
    assert after["weather"] is before["weather"]
    assert after["weather_radar"] is before["weather_radar"]


async def test_location_edit_restarts_location_users(manager, config, monkeypatch):
    bus = Bus()
    await started(manager, bus, config)
    before = by_name(manager)

    async def down(self):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(WeatherRadarCollector, "fetch", down)
    config = copy.deepcopy(config)
    config["modules"]["weather"]["latitude"] = 28.5
    assert sorted(await manager.apply(bus, config)) == ["weather", "weather_radar"]
    after = by_name(manager)
    assert after["news"] is before["news"]
    assert after["weather_radar"].latitude == 28.5
    # The replacement's first failure marks the *previous* instance's payload
    # (still on screen) stale, instead of leaving it looking fresh.
    await until(lambda: bus.payloads["weather_radar"].stale)
    assert bus.payloads["weather_radar"].stage["instance"] == id(before["weather_radar"])


async def test_disabling_stops_and_removes_the_module(manager, config):
    bus = Bus()
    await started(manager, bus, config)
    old_task = manager._tasks["weather_radar"]
    queue = bus.subscribe()
    config = copy.deepcopy(config)
    config["modules"]["weather_radar"]["enabled"] = False
    assert await manager.apply(bus, config) == []
    assert old_task.cancelled()
    assert "weather_radar" not in by_name(manager)
    assert "weather_radar" not in bus.payloads
    assert manager.skipped["weather_radar"] == "disabled"
    messages = [queue.get_nowait() for _ in range(queue.qsize())]
    assert {"type": "module_removed", "module": "weather_radar"} in messages


async def test_constructor_error_stops_the_running_collector(manager, config):
    bus = Bus()
    await started(manager, bus, config)
    config = copy.deepcopy(config)
    config["modules"]["news"]["keep"] = "lots"
    await manager.apply(bus, config)
    assert "news" not in by_name(manager)
    assert "news" not in bus.payloads
    assert manager.skipped["news"].startswith("error: ValueError")
    rows = {r["name"]: r for r in manager.status()}
    assert rows["news"]["state"] == "error"
    assert rows["news"]["detail"].startswith("ValueError")


async def test_dead_task_is_reported_and_restarted(manager, config):
    bus = Bus()
    await started(manager, bus, config)
    task = manager._tasks["news"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    rows = {r["name"]: r for r in manager.status()}
    assert rows["news"]["state"] == "dead" and rows["news"]["stuck"] is True
    assert await manager.apply(bus, config) == ["news"]
    assert not manager._tasks["news"].done()


async def test_status_rows_for_skipped_collectors(manager, config):
    bus = Bus()
    config["modules"]["proxmox"]["enabled"] = True  # no PVE env in tests
    await started(manager, bus, config)
    rows = {r["name"]: r for r in manager.status()}
    assert rows["adsb"] == {"name": "adsb", "state": "disabled", "detail": None}
    assert rows["proxmox"] == {
        "name": "proxmox",
        "state": "missing env",
        "detail": "PVE_URL, PVE_TOKEN_ID, PVE_TOKEN_SECRET",
    }
    assert rows["news"]["state"] == "running"


async def test_concurrent_applies_do_not_leak_tasks(manager, config):
    bus = Bus()
    other = copy.deepcopy(config)
    other["modules"]["news"]["keep"] = 5
    await asyncio.gather(manager.apply(bus, config), manager.apply(bus, other))
    assert set(manager._tasks) == set(RUNNING)
    assert by_name(manager)["news"].keep == 5
    live = [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("collector:")]
    assert len(live) == len(RUNNING)


async def test_stop_cancels_everything(stubbed, config):
    manager = CollectorManager()
    bus = Bus()
    await started(manager, bus, config)
    tasks = list(manager._tasks.values())
    await manager.stop()
    assert all(t.done() for t in tasks)
    assert manager.collectors == [] and manager._tasks == {}


async def test_fixture_mode_manager_never_starts_collectors(stubbed, config):
    manager = CollectorManager(enabled=False)
    bus = Bus()
    assert await manager.apply(bus, config) == []
    assert manager._tasks == {} and stubbed == {}
