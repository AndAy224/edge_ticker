"""backend/collectors/base.py: the poll loop, interval floor, fingerprints, health."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from backend.collectors import base
from backend.collectors.airquality import AirQualityCollector
from backend.collectors.base import MIN_INTERVAL_SECONDS, Collector
from backend.collectors.news import NewsCollector
from backend.collectors.weather_radar import WeatherRadarCollector
from backend.state import ModulePayload
from helpers import FakeBus


class Scripted(Collector):
    """fetch() plays a script: a value is returned, an exception raised, and
    HANG never answers (until cancelled)."""

    name = "scripted"

    def __init__(self, config=None, script=(), interval=60.0) -> None:
        super().__init__(config or {"modules": {}})
        self.interval = interval
        self.script = list(script)
        self.hanging = asyncio.Event()  # set while fetch() sits in a HANG step
        self.cancelled_in_fetch = 0

    async def fetch(self):
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step is HANG:
            self.hanging.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled_in_fetch += 1
                raise
        return step

    def shape(self, raw):
        return ModulePayload(module=self.name, stage={"value": raw})


HANG = object()


class Stop(Exception):
    pass


def fake_sleeps(monkeypatch, count: int) -> list[float]:
    """Swap base's asyncio for one whose sleep() records the delay, and ends
    the loop (CancelledError) on the `count`-th sleep. The fetch deadline
    stays real."""
    delays: list[float] = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) >= count:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        base,
        "asyncio",
        SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError, timeout=asyncio.timeout),
    )
    return delays


async def run_loop(collector: Collector, bus) -> None:
    with pytest.raises(asyncio.CancelledError):
        await collector.start(bus)


async def test_success_publishes_and_sleeps_interval(monkeypatch, fake_bus):
    delays = fake_sleeps(monkeypatch, 1)
    collector = Scripted(script=[42], interval=60)
    await run_loop(collector, fake_bus)
    assert delays == [60]
    payload = fake_bus.payloads["scripted"]
    assert payload.stage == {"value": 42} and payload.stale is False
    assert collector.last_success is not None and collector.last_error is None
    assert collector.last_duration_ms is not None


async def test_zero_interval_is_floored(monkeypatch, fake_bus):
    # A cleared admin field used to save poll_seconds: 0 and spin the loop.
    delays = fake_sleeps(monkeypatch, 3)
    collector = Scripted(script=[1, RuntimeError("x"), RuntimeError("y")], interval=0)
    await run_loop(collector, fake_bus)
    # poll sleep floored to 5; the failure backoff starts at the floor too
    # (min(interval=0, 5) would otherwise be 0) and doubles from there.
    assert delays == [MIN_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, 2 * MIN_INTERVAL_SECONDS]


async def test_failure_backoff_doubles_and_caps(monkeypatch, fake_bus):
    delays = fake_sleeps(monkeypatch, 9)
    collector = Scripted(script=[RuntimeError("down")] * 9, interval=600)
    await run_loop(collector, fake_bus)
    assert delays == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]


async def test_backoff_start_never_exceeds_interval(monkeypatch, fake_bus):
    delays = fake_sleeps(monkeypatch, 1)
    collector = Scripted(script=[RuntimeError("down")], interval=60)
    collector.backoff_start = 900.0  # e.g. launches
    await run_loop(collector, fake_bus)
    assert delays == [60]


async def test_success_resets_backoff_and_counters(monkeypatch, fake_bus):
    delays = fake_sleeps(monkeypatch, 5)
    collector = Scripted(
        script=[RuntimeError("a"), RuntimeError("b"), 1, RuntimeError("c"), 2], interval=30
    )
    await run_loop(collector, fake_bus)
    assert delays == [5.0, 10.0, 30, 5.0, 30]
    assert collector.failures_total == 3
    assert collector.consecutive_failures == 0
    assert collector.stale is False


async def test_failure_marks_last_payload_stale_once(monkeypatch, fake_bus):
    fake_sleeps(monkeypatch, 3)
    collector = Scripted(script=[7, RuntimeError("boom"), RuntimeError("boom")])
    await run_loop(collector, fake_bus)
    published = [m["payload"] for m in fake_bus.of_type("module")]
    assert [p.stale for p in published] == [False, True]  # not re-published per failure
    assert published[1].stage == {"value": 7}
    assert collector.last_error == "RuntimeError: boom"
    assert collector.failures_total == 2 and collector.consecutive_failures == 2
    assert collector.stale is True


async def test_restarted_collector_inherits_payload_from_bus(monkeypatch, fake_bus):
    # A config save replaces the instance; its first failure must still mark
    # what's on screen stale.
    fake_bus.payloads["scripted"] = ModulePayload(module="scripted", stage={"value": "old"})
    fake_sleeps(monkeypatch, 1)
    collector = Scripted(script=[RuntimeError("down")])
    await run_loop(collector, fake_bus)
    published = [m["payload"] for m in fake_bus.of_type("module")]
    assert len(published) == 1
    assert published[0].stale is True and published[0].stage == {"value": "old"}


async def test_failure_without_any_payload_publishes_nothing(monkeypatch, fake_bus):
    fake_sleeps(monkeypatch, 1)
    await run_loop(Scripted(script=[RuntimeError("down")]), fake_bus)
    assert fake_bus.messages == []


async def test_cancellation_propagates_out_of_fetch(fake_bus):
    collector = Scripted(script=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await collector.start(fake_bus)
    assert collector.failures_total == 0


# ---- the fetch deadline -------------------------------------------------------------


async def test_hung_fetch_becomes_an_ordinary_failure(monkeypatch, fake_bus):
    # An upstream trickling bytes never trips a per-request timeout: the fetch
    # used to sit until /health called the collector stuck and the watchdog
    # restarted the whole backend.
    delays = fake_sleeps(monkeypatch, 3)
    collector = Scripted(script=[7, HANG, 8], interval=60)
    collector.fetch_timeout = 0.05
    await run_loop(collector, fake_bus)
    published = [m["payload"] for m in fake_bus.of_type("module")]
    assert [(p.stage["value"], p.stale) for p in published] == [(7, False), (7, True), (8, False)]
    assert delays == [60, 5.0, 60]  # the usual backoff, then back to the interval
    assert collector.cancelled_in_fetch == 1  # the hung fetch was torn down, not abandoned
    assert collector.failures_total == 1 and collector.consecutive_failures == 0
    assert collector._attempt_started is None and not collector.stuck()


async def test_fetch_deadline_is_reported_as_the_error(monkeypatch, fake_bus):
    fake_sleeps(monkeypatch, 1)
    collector = Scripted(script=[HANG])
    collector.fetch_timeout = 0.05
    await run_loop(collector, fake_bus)
    assert collector.last_error == "TimeoutError: fetch() still running after 0.05 s"
    assert collector.stale is True


async def test_a_timeout_raised_by_fetch_itself_keeps_its_own_message(monkeypatch, fake_bus):
    fake_sleeps(monkeypatch, 1)
    collector = Scripted(script=[TimeoutError("upstream took too long")])
    await run_loop(collector, fake_bus)
    assert collector.last_error == "TimeoutError: upstream took too long"


async def test_stopping_a_collector_mid_fetch_is_a_cancellation_not_a_timeout(fake_bus):
    # The manager stops a collector by cancelling its task.
    collector = Scripted(script=[HANG])
    task = asyncio.create_task(collector.start(fake_bus))
    await asyncio.wait_for(collector.hanging.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert collector.cancelled_in_fetch == 1
    assert collector.failures_total == 0 and collector.last_error is None
    assert fake_bus.messages == []


def test_every_fetch_deadline_fires_before_the_collector_counts_as_stuck():
    from backend.collectors import collector_classes

    for cls in collector_classes():
        assert 0 < cls.fetch_timeout < base.HUNG_ATTEMPT_SECONDS, cls.name
    assert Collector.fetch_timeout <= base.HUNG_ATTEMPT_SECONDS / 2


# ---- pruning a stale payload ---------------------------------------------------------


class Expiring(Scripted):
    """Stage values listed in `expired` have run out on their own clock."""

    name = "expiring"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.expired: set[str] = set()
        self.prune_calls = 0

    def prune_stale(self, payload):
        self.prune_calls += 1
        kept = [v for v in payload.stage["value"] if v not in self.expired]
        if len(kept) == len(payload.stage["value"]):
            return payload
        return payload.model_copy(update={"stage": {"value": kept}})


async def test_stale_republish_drops_what_expired_and_repeats_only_on_change(monkeypatch, fake_bus):
    collector = Expiring(script=[["a", "b", "c"]] + [RuntimeError("down")] * 4)
    expiries = iter([set(), {"a"}, {"a"}, {"a", "b"}, {"a", "b"}])
    delays: list[float] = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            raise asyncio.CancelledError
        collector.expired = next(expiries)  # time passes between polls

    monkeypatch.setattr(
        base,
        "asyncio",
        SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError, timeout=asyncio.timeout),
    )
    await run_loop(collector, fake_bus)
    published = [m["payload"] for m in fake_bus.of_type("module")]
    # fresh; stale as it was; "a" ran out; (no change: nothing); "b" ran out.
    assert [(p.stage["value"], p.stale) for p in published] == [
        (["a", "b", "c"], False),
        (["a", "b", "c"], True),
        (["b", "c"], True),
        (["c"], True),
    ]
    assert collector.prune_calls == 4  # every failed poll
    assert published[-1].updated_at == published[0].updated_at  # still the last good fetch's age


async def test_prune_hook_that_raises_does_not_break_the_failure_path(monkeypatch, fake_bus):
    class Broken(Scripted):
        def prune_stale(self, payload):
            raise KeyError("stage changed shape")

    delays = fake_sleeps(monkeypatch, 3)
    collector = Broken(script=[7, RuntimeError("down"), RuntimeError("down")])
    await run_loop(collector, fake_bus)
    published = [m["payload"] for m in fake_bus.of_type("module")]
    assert [(p.stage["value"], p.stale) for p in published] == [(7, False), (7, True)]
    assert delays == [60, 5.0, 10.0]  # the loop kept its pacing
    assert collector.last_error == "RuntimeError: down"


async def test_inherited_stale_payload_is_pruned_by_a_restarted_collector(monkeypatch, fake_bus):
    fake_bus.payloads["expiring"] = ModulePayload(module="expiring", stage={"value": ["a", "b"]}, stale=True)
    fake_sleeps(monkeypatch, 1)
    collector = Expiring(script=[RuntimeError("down")])
    collector.expired = {"a"}
    await run_loop(collector, fake_bus)
    [message] = fake_bus.of_type("module")
    assert message["payload"].stage == {"value": ["b"]} and message["payload"].stale is True


# ---- config fingerprints ----------------------------------------------------------


def test_fingerprint_tracks_own_module_only(defaults_config):
    before = NewsCollector.config_fingerprint(defaults_config)
    changed = copy.deepcopy(defaults_config)
    changed["modules"]["weather"]["latitude"] = 40.0
    changed["appearance"]["theme"] = "frost"
    changed["modules"]["sports"]["days_back"] = 2
    assert NewsCollector.config_fingerprint(changed) == before
    changed["modules"]["news"]["keep"] = 10
    assert NewsCollector.config_fingerprint(changed) != before


@pytest.mark.parametrize("key,value", [("latitude", 40.0), ("longitude", -80.0), ("location_name", "Tampa")])
def test_location_users_follow_the_weather_location(defaults_config, key, value):
    changed = copy.deepcopy(defaults_config)
    changed["modules"]["weather"][key] = value
    for cls in (AirQualityCollector, WeatherRadarCollector):
        assert cls.uses_location
        assert cls.config_fingerprint(changed) != cls.config_fingerprint(defaults_config)


def test_location_users_ignore_other_weather_keys(defaults_config):
    changed = copy.deepcopy(defaults_config)
    changed["modules"]["weather"]["poll_seconds"] = 60
    assert WeatherRadarCollector.config_fingerprint(changed) == WeatherRadarCollector.config_fingerprint(
        defaults_config
    )


def test_every_collector_reading_the_weather_location_declares_it():
    import inspect

    from backend.collectors import collector_classes

    for cls in collector_classes():
        if cls.name == "weather":
            continue
        source = inspect.getsource(cls.__init__)
        if '.get("weather"' in source:
            assert cls.uses_location, f"{cls.name} reads modules.weather but uses_location is False"


def test_fingerprint_tolerates_missing_or_null_modules():
    assert NewsCollector.config_fingerprint({}) == NewsCollector.config_fingerprint({"modules": None})
    assert WeatherRadarCollector.config_fingerprint({"modules": {"weather": None}})


# ---- health: stuck / overdue / status ------------------------------------------------


class Clock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(base, "time", SimpleNamespace(monotonic=clock))
    return clock


def test_stuck_when_an_attempt_hangs(clock):
    collector = Scripted(interval=60)
    collector._attempt_started = clock.now
    clock.now += base.HUNG_ATTEMPT_SECONDS - 1
    assert not collector.stuck()
    clock.now += 2
    assert collector.stuck()


def test_stuck_when_the_next_attempt_is_long_overdue(clock):
    collector = Scripted(interval=60)  # longest sleep = backoff_max (300)
    clock.now += 300 + 119
    assert not collector.stuck()
    clock.now += 2
    assert collector.stuck()


def test_stuck_threshold_respects_long_backoff(clock):
    collector = Scripted(interval=60)
    collector.backoff_max = 3600.0  # launches-style
    clock.now += 3000
    assert not collector.stuck()


def test_overdue_measured_from_last_success(clock, monkeypatch):
    from datetime import datetime, timedelta, timezone

    collector = Scripted(interval=60)
    assert not collector.overdue()  # never attempted: not overdue
    collector.last_success = datetime.now(timezone.utc) - timedelta(seconds=3 * 60 + 30)
    assert not collector.overdue()
    collector.last_success = datetime.now(timezone.utc) - timedelta(seconds=3 * 60 + 90)
    assert collector.overdue()


def test_status_fields(clock):
    collector = Scripted(interval=45)
    status = collector.status()
    assert status["name"] == "scripted" and status["state"] == "running"
    for key in (
        "interval", "stale", "overdue", "stuck", "degraded", "last_success", "last_error",
        "failures_total", "consecutive_failures", "last_duration_ms",
    ):
        assert key in status
    collector.degraded = "1/4 feeds failing"
    assert collector.status()["degraded"] == "1/4 feeds failing"


def test_never_succeeded_collector_becomes_overdue(clock):
    collector = Scripted(interval=60)  # started at clock.now
    # Fails every 5 minutes for an hour; each attempt ends a moment ago.
    for _ in range(12):
        clock.now += 300
        collector._attempt_started = clock.now - 1
        collector.failures_total += 1
        collector.consecutive_failures += 1
        collector._end_attempt()
    assert collector.last_success is None
    assert collector.overdue()
