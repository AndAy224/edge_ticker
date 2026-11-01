"""CameraAlertHub's Home Assistant edge detection."""
from __future__ import annotations

from backend.camera_alert import CameraAlertHub
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
