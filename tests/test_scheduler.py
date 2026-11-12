"""backend/scheduler.py: level-triggered night dimming, reload, state(), ddcutil."""
from __future__ import annotations

import asyncio
from datetime import datetime as real_datetime

import pytest

from backend import scheduler as scheduler_module
from backend.scheduler import NightScheduler, _in_night_window
from backend.state import Bus
from helpers import FakeBus, until

NIGHT = {
    "dim_at": "23:00",
    "wake_at": "07:00",
    "dim_level": 10,
    "day_level": 100,
    "method": "ddc",
    "nightly_reload_at": "04:00",
}


class Ddc:
    """Stands in for `ddcutil setvcp`: records levels, returns `works`.

    `fail_next` fails that many calls whatever `works` says; `delay` makes each
    call take that long; while `gate` is set, calls wait for it to open."""

    def __init__(self, works: bool = True) -> None:
        self.works = works
        self.levels: list[int] = []
        self.fail_next = 0
        self.delay = 0.0
        self.gate: asyncio.Event | None = None

    async def __call__(self, level: int) -> bool:
        self.levels.append(level)
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_next:
            self.fail_next -= 1
            return False
        return self.works


@pytest.fixture
def night():
    return dict(NIGHT)


@pytest.fixture
def make(fake_bus, monkeypatch, night):
    def build(works: bool = True):
        sched = NightScheduler(fake_bus, lambda: {"night": night})
        ddc = Ddc(works)
        monkeypatch.setattr(sched, "_ddcutil", ddc)
        return sched, ddc

    return build


def pin_clock(monkeypatch, hour: int, minute: int) -> None:
    class Fixed(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 9, 25, hour, minute, tzinfo=tz)

    monkeypatch.setattr(scheduler_module, "datetime", Fixed)


@pytest.mark.parametrize(
    "dim_at,wake_at,minute,expected",
    [
        ("23:00", "07:00", "23:00", True),
        ("23:00", "07:00", "03:15", True),
        ("23:00", "07:00", "06:59", True),
        ("23:00", "07:00", "07:00", False),
        ("23:00", "07:00", "22:59", False),
        ("01:00", "05:00", "03:00", True),  # same-day window
        ("01:00", "05:00", "05:00", False),
        ("01:00", "05:00", "00:30", False),
        ("22:00", "22:00", "22:00", False),  # empty window
        (None, "07:00", "03:00", False),
        ("23:00", "", "03:00", False),
    ],
)
def test_night_window(dim_at, wake_at, minute, expected):
    assert _in_night_window({"dim_at": dim_at, "wake_at": wake_at}, minute) is expected


async def test_startup_tick_always_reconciles(make, fake_bus):
    sched, ddc = make()
    await sched._tick("12:00")
    assert ddc.levels == [100]  # day level applied even though no edge was crossed
    assert sched.method_used == "ddc"
    # Hardware handles it; the display is told there is no software dim to draw.
    assert fake_bus.of_type("night") == [
        {"type": "night", "mode": "wake", "level": 100, "software": False}
    ]


async def test_restart_inside_the_window_dims_immediately(make):
    sched, ddc = make()
    await sched._tick("02:30")
    assert ddc.levels == [10]


async def test_level_triggered_only_on_change(make):
    sched, ddc = make()
    for minute in ("22:58", "22:59", "23:00", "23:01", "06:59", "07:00", "07:01"):
        await sched._tick(minute)
    assert ddc.levels == [100, 10, 100]


async def test_admin_edits_apply_on_the_next_minute(make, night):
    sched, ddc = make()
    await sched._tick("23:30")
    night["dim_level"] = 25
    await sched._tick("23:31")
    night["method"] = "software"
    await sched._tick("23:32")
    # Leaving DDC puts the backlight back at the day level (once): the
    # software overlay does the dimming from here.
    assert ddc.levels == [10, 25, 100]


async def test_leaving_ddc_does_not_strand_the_backlight_dimmed(make, fake_bus, night):
    sched, ddc = make()
    await sched._tick("23:30")
    assert ddc.levels == [10]
    night["method"] = "software"
    await sched._tick("23:31")
    # Software mode never runs ddcutil, so without this one write the morning
    # wake only lifted the overlay and the panel stayed at 10% for good.
    assert ddc.levels == [10, 100]
    assert fake_bus.of_type("night")[-1] == {"type": "night", "mode": "dim", "level": 10, "software": True}
    for minute in ("23:32", "03:00", "07:00", "12:00"):
        await sched._tick(minute)
    assert ddc.levels == [10, 100]
    assert fake_bus.of_type("night")[-1] == {"type": "night", "mode": "wake", "level": 100, "software": True}


async def test_software_mode_from_the_start_never_runs_ddcutil(make, night):
    night["method"] = "software"
    sched, ddc = make()
    for minute in ("12:00", "23:00", "07:00"):
        await sched._tick(minute)
    assert ddc.levels == []


async def test_software_fallback_broadcasts_night(make, fake_bus):
    sched, ddc = make(works=False)
    await sched._tick("23:00")
    assert ddc.levels == [10]
    assert sched.method_used == "software"
    assert fake_bus.of_type("night") == [
        {"type": "night", "mode": "dim", "level": 10, "software": True}
    ]
    await sched._tick("23:01")  # no change: no rebroadcast
    assert len(fake_bus.of_type("night")) == 1
    await sched._tick("07:00")
    assert fake_bus.of_type("night")[-1] == {
        "type": "night", "mode": "wake", "level": 100, "software": True
    }


async def test_software_method_skips_ddcutil(make, fake_bus, night):
    night["method"] = "software"
    sched, ddc = make()
    await sched._tick("23:00")
    assert ddc.levels == []
    assert fake_bus.of_type("night") == [
        {"type": "night", "mode": "dim", "level": 10, "software": True}
    ]


async def test_nightly_reload(make, fake_bus):
    sched, _ = make()
    await sched._tick("03:59")
    await sched._tick("04:00")
    await sched._tick("04:01")
    assert fake_bus.of_type("control") == [{"type": "control", "action": "reload"}]


async def test_state_reflects_the_clock_and_method(make, monkeypatch, night):
    sched, _ = make(works=False)
    pin_clock(monkeypatch, 12, 0)
    assert sched.state() == {"mode": "wake", "level": 100, "software": False}
    pin_clock(monkeypatch, 23, 30)
    assert sched.state() == {"mode": "dim", "level": 10, "software": False}
    await sched._tick("23:30")  # ddc failed -> the display has to dim itself
    assert sched.state() == {"mode": "dim", "level": 10, "software": True}


async def test_boost_raises_then_restores(make, monkeypatch):
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    sched.boost(0.01)
    await sched._boost
    assert ddc.levels == [100, 10]


async def test_boost_returns_before_ddcutil_does(make, monkeypatch):
    # Camera takeovers boost from inside the HA bridge's reader: a boost that
    # waited on ddcutil (up to 20 s) held up the WebRTC answers behind it.
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    ddc.gate = asyncio.Event()
    assert sched.boost(0.01) is None  # not a coroutine: nothing to wait on
    await until(lambda: ddc.levels == [100])
    assert not sched._boost.done()
    ddc.gate.set()
    await sched._boost
    assert ddc.levels == [100, 10]


async def test_boost_is_a_noop_by_day_or_in_software_mode(make, monkeypatch, night):
    sched, ddc = make()
    pin_clock(monkeypatch, 12, 0)
    sched.boost(0.01)
    night["method"] = "software"
    pin_clock(monkeypatch, 23, 30)
    sched.boost(0.01)
    assert sched._boost is None
    assert ddc.levels == []


async def test_boost_is_a_noop_while_the_display_draws_the_dim(make, monkeypatch):
    # DDC failed at dim_at, so the display dims in software and lifts that
    # itself for a takeover. A boost would only run ddcutil to fail again, or,
    # succeeding, restore the backlight to dim_level under the overlay.
    sched, ddc = make(works=False)
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    sched.boost(0.01)
    assert sched._boost is None
    assert ddc.levels == [10]


async def test_tick_cancels_a_pending_boost_restore(make, monkeypatch, night):
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    sched.boost(60)
    await until(lambda: ddc.levels == [10, 100])
    night["dim_level"] = 20
    await sched._tick("23:31")
    assert sched._boost.cancelled()
    assert ddc.levels == [10, 100, 20]


async def test_tick_leaves_a_running_boost_alone(make, monkeypatch):
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    sched.boost(60)
    await until(lambda: ddc.levels == [10, 100])
    await sched._tick("23:31")  # nothing changed: the takeover keeps its brightness
    assert ddc.levels == [10, 100]
    assert not sched._boost.done()
    await sched._end_boost()


async def test_a_second_takeover_extends_the_boost(make, monkeypatch):
    # dim -> boost -> a second takeover within the hold. The second boost used
    # to cancel the first's restore and run ddcutil again; when that call
    # failed it scheduled no restore, and the panel stayed at 100% all night.
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    sched.boost(0.3)
    await until(lambda: ddc.levels == [10, 100])
    ddc.fail_next = 1  # the transient failure the second boost's call met
    sched.boost(0.4)
    await asyncio.sleep(0.05)
    assert ddc.levels == [10, 100]  # already bright: nothing to write
    ddc.fail_next = 0
    await sched._boost
    assert ddc.levels == [10, 100, 10] and sched._hw == 10


async def test_concurrent_boosts_share_one_restore(make, monkeypatch):
    # Two boosts in flight at once each scheduled a restore; the orphan
    # re-dimmed the panel in the middle of the later, longer takeover.
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    ddc.delay = 0.02
    sched.boost(0.05)
    sched.boost(0.4)
    await asyncio.sleep(0.2)
    assert ddc.levels == [10, 100]  # still bright while the second takeover is up
    await sched._boost
    assert ddc.levels == [10, 100, 10]


async def test_a_failed_restore_is_retried_by_the_tick(make, monkeypatch):
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    sched.boost(0.3)
    await until(lambda: ddc.levels == [10, 100])
    ddc.works = False
    await sched._boost
    assert ddc.levels == [10, 100, 10] and sched._hw is None  # the restore failed
    ddc.works = True
    await sched._tick("23:31")  # `_applied` still says dim; the backlight doesn't
    assert ddc.levels == [10, 100, 10, 10] and sched._hw == 10
    await sched._tick("23:32")
    assert len(ddc.levels) == 4


async def test_a_restore_that_keeps_failing_falls_back_to_the_software_dim(make, monkeypatch, fake_bus):
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    sched.boost(0.3)
    await until(lambda: ddc.levels == [10, 100])
    ddc.works = False
    await sched._boost
    await sched._tick("23:31")  # retried once, failed again
    assert fake_bus.of_type("night")[-1] == {"type": "night", "mode": "dim", "level": 10, "software": True}
    await sched._tick("23:32")  # and no ddcutil every minute after that
    assert ddc.levels == [10, 100, 10, 10]


async def test_a_boost_waits_for_the_ticks_write(make, monkeypatch):
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    ddc.gate = asyncio.Event()
    tick = asyncio.create_task(sched._tick("23:30"))
    await until(lambda: ddc.levels == [10])
    sched.boost(0.01)
    await asyncio.sleep(0.02)
    assert ddc.levels == [10]  # one ddcutil on the i2c bus at a time
    ddc.gate.set()
    await tick
    await sched._boost
    assert ddc.levels == [10, 100, 10]


async def test_one_bad_weather_boost_does_not_end_the_watcher(monkeypatch):
    bus = Bus()
    sched = NightScheduler(bus, lambda: {"night": dict(NIGHT)})
    calls = []

    def boost(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            raise ValueError("bad night config")

    monkeypatch.setattr(sched, "boost", boost)
    watcher = asyncio.create_task(sched._watch_takeovers())
    await until(lambda: bus._internal)
    await bus.broadcast({"type": "weather_alert"})
    await bus.broadcast({"type": "module"})
    await bus.broadcast({"type": "weather_alert"})
    await until(lambda: len(calls) == 2)
    assert not watcher.done()
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)


async def test_ddc_recovery_clears_the_software_dim(make, fake_bus):
    sched, ddc = make(works=False)
    await sched._tick("23:00")  # ddcutil flaked: software dim at 10%
    ddc.works = True
    await sched._tick("07:00")  # ddcutil back: panel at 100% via DDC
    last = fake_bus.of_type("night")[-1]
    assert last["mode"] == "wake"


# ---- the real _ddcutil, with the subprocess faked -----------------------------------


class HangingProc:
    killed = False

    async def wait(self):
        await asyncio.sleep(3600)

    def kill(self):
        self.killed = True


async def test_cancelled_ddcutil_kills_its_child(monkeypatch):
    # A boost cancelled by the minute tick left ddcutil driving the i2c bus
    # behind the write that replaced it; only the timeout path killed it.
    proc = HangingProc()

    async def spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(scheduler_module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(NightScheduler._ddcutil(40))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


async def test_ddcutil_times_out_and_kills(monkeypatch):
    proc = HangingProc()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return proc

    monkeypatch.setattr(scheduler_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(scheduler_module, "DDCUTIL_TIMEOUT_SECONDS", 0.01)
    assert await NightScheduler._ddcutil(40) is False
    assert proc.killed
    assert calls == [("ddcutil", "setvcp", "10", "40")]


@pytest.mark.parametrize("returncode,expected", [(0, True), (1, False)])
async def test_ddcutil_exit_status(monkeypatch, returncode, expected):
    class Proc:
        async def wait(self):
            return returncode

    async def spawn(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(scheduler_module.asyncio, "create_subprocess_exec", spawn)
    assert await NightScheduler._ddcutil(40) is expected


async def test_ddcutil_missing_binary(monkeypatch):
    async def spawn(*args, **kwargs):
        raise FileNotFoundError("ddcutil")

    monkeypatch.setattr(scheduler_module.asyncio, "create_subprocess_exec", spawn)
    assert await NightScheduler._ddcutil(40) is False
