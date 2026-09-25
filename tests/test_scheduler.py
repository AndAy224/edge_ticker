"""backend/scheduler.py: level-triggered night dimming, reload, state(), ddcutil."""
from __future__ import annotations

import asyncio
from datetime import datetime as real_datetime

import pytest

from backend import scheduler as scheduler_module
from backend.scheduler import NightScheduler, _in_night_window
from helpers import FakeBus

NIGHT = {
    "dim_at": "23:00",
    "wake_at": "07:00",
    "dim_level": 10,
    "day_level": 100,
    "method": "ddc",
    "nightly_reload_at": "04:00",
}


class Ddc:
    """Stands in for `ddcutil setvcp`: records levels, returns `works`."""

    def __init__(self, works: bool = True) -> None:
        self.works = works
        self.levels: list[int] = []

    async def __call__(self, level: int) -> bool:
        self.levels.append(level)
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
    assert ddc.levels == [10, 25]  # the software switch doesn't touch ddcutil


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
    await sched.boost(0.01)
    assert ddc.levels == [100]
    await sched._boost
    assert ddc.levels == [100, 10]


async def test_boost_is_a_noop_by_day_or_in_software_mode(make, monkeypatch, night):
    sched, ddc = make()
    pin_clock(monkeypatch, 12, 0)
    await sched.boost(0.01)
    night["method"] = "software"
    pin_clock(monkeypatch, 23, 30)
    await sched.boost(0.01)
    assert ddc.levels == []


async def test_tick_cancels_a_pending_boost_restore(make, monkeypatch, night):
    sched, ddc = make()
    pin_clock(monkeypatch, 23, 30)
    await sched._tick("23:30")
    await sched.boost(60)
    night["dim_level"] = 20
    await sched._tick("23:31")
    await asyncio.sleep(0)
    assert sched._boost.cancelled() or sched._boost.done()
    assert ddc.levels == [10, 100, 20]


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
