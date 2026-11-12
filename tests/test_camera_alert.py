"""CameraAlertHub: Home Assistant edge detection, durations and the cooldown."""
from __future__ import annotations

import pytest

from backend.camera_alert import (
    DEFAULT_DURATION,
    MAX_DURATION,
    MIN_DURATION,
    CameraAlertHub,
    _clamp_int,
)
from helpers import FakeBus


def hub_for(alerts: list[dict]) -> tuple[CameraAlertHub, FakeBus]:
    bus = FakeBus()
    config = {"ha": {"alerts": alerts}}
    return CameraAlertHub(bus, lambda: config), bus


JAMMED = {"entity": "lock.front", "state": "jammed", "text": "Front lock jammed"}
UNLOCKED = {"entity": "lock.front", "state": "unlocked", "takeover": True,
            "cameras": ["camera.porch"], "cooldown_seconds": 0}


async def test_takeover_fires_behind_another_alert_on_the_same_entity():
    # the tape-only alert listed first used to end the scan with `return`
    hub, bus = hub_for([JAMMED, UNLOCKED])
    await hub.on_ha_state_change("lock.front", "locked", "unlocked")
    assert len(bus.of_type("camera_alert")) == 1


async def test_takeover_fires_in_either_order():
    hub, bus = hub_for([UNLOCKED, JAMMED])
    await hub.on_ha_state_change("lock.front", "locked", "unlocked")
    assert len(bus.of_type("camera_alert")) == 1


async def test_non_takeover_state_does_not_fire():
    hub, bus = hub_for([JAMMED, UNLOCKED])
    await hub.on_ha_state_change("lock.front", "locked", "jammed")
    assert bus.of_type("camera_alert") == []


# ---- fire(): durations and the cooldown -------------------------------------------


@pytest.mark.parametrize(
    "value, seconds",
    [
        (45, 45), (45.9, 45), ("45", 45), ("45.9", 45),
        (0, MIN_DURATION), (-1e999, MIN_DURATION), (1e999, MAX_DURATION),
        (10**400, MAX_DURATION), ("1e999", MAX_DURATION),
        (float("nan"), DEFAULT_DURATION), ("nan", DEFAULT_DURATION),
        (None, DEFAULT_DURATION), ("soon", DEFAULT_DURATION), ("", DEFAULT_DURATION),
        (True, DEFAULT_DURATION), ([30], DEFAULT_DURATION), ({}, DEFAULT_DURATION),
    ],
)
def test_clamp_int_has_an_answer_for_anything(value, seconds):
    # int(inf) raises OverflowError, which fire() let out as a 500
    assert _clamp_int(value, DEFAULT_DURATION, MIN_DURATION, MAX_DURATION) == seconds


async def test_a_fire_that_goes_out_starts_the_cooldown():
    hub, bus = hub_for([])
    fire = dict(key="door", source="test", title="Door", cameras=["camera.porch"])
    assert await hub.fire(**fire) is not None
    assert await hub.fire(**fire) is None
    assert len(bus.of_type("camera_alert")) == 1


async def test_a_fire_with_no_usable_cameras_does_not_start_the_cooldown():
    # it used to: the real takeover right behind it was then suppressed
    hub, bus = hub_for([])
    assert await hub.fire(key="door", source="test", title="Door", cameras=["light.porch"]) is None
    event = await hub.fire(key="door", source="test", title="Door", cameras=["camera.porch"])
    assert event is not None and len(bus.of_type("camera_alert")) == 1


async def test_a_fire_that_fails_does_not_start_the_cooldown():
    hub, bus = hub_for([])
    hub.friendly_name = lambda entity_id: 1 / 0
    with pytest.raises(ZeroDivisionError):
        await hub.fire(key="door", source="test", title="Door", cameras=["camera.porch"])
    hub.friendly_name = lambda entity_id: entity_id
    assert await hub.fire(key="door", source="test", title="Door", cameras=["camera.porch"])
    assert len(bus.of_type("camera_alert")) == 1
