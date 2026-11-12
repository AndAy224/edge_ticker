"""backend/ha_bridge.py: the service-call allowlist and state fan-out."""
from __future__ import annotations

import asyncio
from datetime import datetime as real_datetime

import pytest

from backend import ha_bridge
from backend import scheduler as scheduler_module
from backend.camera_alert import CameraAlertHub
from backend.ha_bridge import CONTROL_SERVICES, HABridge
from backend.scheduler import NightScheduler
from helpers import FakeBus, until

HA_CONFIG = {
    "ha": {
        "scenes": ["scene.movie"],
        "lights": ["light.desk", "switch.porch"],
        "fans": ["fan.bedroom"],
        "climate": "climate.hall",
        "media": "media_player.tv",
        "alerts": [{"entity": "binary_sensor.front_door", "state": "on"}],
    }
}


@pytest.fixture
def bridge(fake_bus, monkeypatch):
    config = {"value": HA_CONFIG}
    bridge = HABridge(fake_bus, lambda: config["value"])
    bridge.sent = []

    async def command(payload):
        bridge.sent.append(payload)
        return {"ok": True}

    monkeypatch.setattr(bridge, "_command", command)
    bridge.set_config = lambda value: config.__setitem__("value", value)
    return bridge


@pytest.mark.parametrize(
    "domain,service,entity",
    [
        ("scene", "turn_on", "scene.movie"),
        ("light", "toggle", "light.desk"),
        ("switch", "turn_off", "switch.porch"),
        ("fan", "set_percentage", "fan.bedroom"),
        ("climate", "set_temperature", "climate.hall"),
        ("media_player", "volume_set", "media_player.tv"),
    ],
)
async def test_allowed_calls_target_only_the_mapped_entity(bridge, domain, service, entity):
    assert await bridge.call_service(domain, service, entity, {"brightness": 128}) == {"ok": True}
    assert bridge.sent == [
        {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": {"brightness": 128},
            "target": {"entity_id": entity},
        }
    ]


async def test_target_keys_are_stripped_from_service_data(bridge):
    data = {
        "entity_id": "lock.front_door",
        "device_id": "abc",
        "area_id": "garage",
        "floor_id": "ground",
        "label_id": "security",
        "percentage": 50,
    }
    await bridge.call_service("fan", "set_percentage", "fan.bedroom", data)
    sent = bridge.sent[0]
    assert sent["service_data"] == {"percentage": 50}
    assert sent["target"] == {"entity_id": "fan.bedroom"}


@pytest.mark.parametrize(
    "domain,service,entity",
    [
        ("lock", "unlock", "lock.front_door"),  # not mapped at all
        ("alarm_control_panel", "alarm_disarm", "light.desk"),  # wrong domain for group
        ("light", "turn_on", "binary_sensor.front_door"),  # alert entities are state-only
        ("scene", "delete", "scene.movie"),  # service not in the allowlist
        ("homeassistant", "restart", "light.desk"),
        ("climate", "set_temperature", "media_player.tv"),
        ("media_player", "play_media", "media_player.tv"),
    ],
)
async def test_everything_else_is_refused(bridge, domain, service, entity):
    with pytest.raises(PermissionError):
        await bridge.call_service(domain, service, entity)
    assert bridge.sent == []


async def test_missing_arguments(bridge):
    with pytest.raises(ValueError):
        await bridge.call_service(None, "turn_on", "light.desk")
    with pytest.raises(ValueError):
        await bridge.call_service("light", "", "light.desk")
    with pytest.raises(PermissionError):
        await bridge.call_service("light", "turn_on", None)
    assert bridge.sent == []


async def test_allowlist_follows_live_config(bridge):
    await bridge.call_service("light", "toggle", "light.desk")
    bridge.set_config({"ha": {"lights": []}})
    with pytest.raises(PermissionError):
        await bridge.call_service("light", "toggle", "light.desk")
    bridge.set_config(None)
    with pytest.raises(PermissionError):
        await bridge.call_service("light", "toggle", "light.desk")


def test_control_services_never_include_dangerous_domains():
    domains = {d for groups in CONTROL_SERVICES.values() for d in groups}
    assert domains.isdisjoint({"lock", "alarm_control_panel", "homeassistant", "script", "shell_command", "cover"})


async def test_call_without_connection_raises(fake_bus):
    bridge = HABridge(fake_bus, lambda: HA_CONFIG)
    with pytest.raises(RuntimeError, match="not connected"):
        await bridge.call_service("light", "toggle", "light.desk")


def test_status_without_env_is_unconfigured(fake_bus):
    assert HABridge(fake_bus, lambda: {}).status == "unconfigured"


async def test_state_changes_fan_out_for_mapped_entities_only(bridge, fake_bus):
    def event(entity, state):
        return {
            "event_type": "state_changed",
            "data": {
                "entity_id": entity,
                "new_state": {
                    "state": state,
                    "attributes": {"friendly_name": entity, "brightness": 10, "secret_token": "x"},
                },
            },
        }

    await bridge._on_event(event("light.desk", "on"))
    await bridge._on_event(event("binary_sensor.front_door", "on"))  # alert: mapped
    await bridge._on_event(event("lock.front_door", "unlocked"))  # unmapped
    broadcast = fake_bus.of_type("ha_state")
    assert [m["entity_id"] for m in broadcast] == ["light.desk", "binary_sensor.front_door"]
    assert broadcast[0]["attributes"] == {"friendly_name": "light.desk", "brightness": 10}
    assert set(bridge.all_states) == {"light.desk", "binary_sensor.front_door", "lock.front_door"}
    assert set(bridge.mapped_states()) == {"light.desk", "binary_sensor.front_door"}


def test_list_entities_sorted_by_domain_then_name(bridge):
    bridge.all_states = {
        "light.b": {"state": "on", "attributes": {"friendly_name": "beta"}},
        "fan.x": {"state": "off", "attributes": {}},
        "light.a": {"state": "off", "attributes": {"friendly_name": "Alpha"}},
    }
    assert [e["entity_id"] for e in bridge.list_entities()] == ["fan.x", "light.a", "light.b"]


# ---- connection robustness -----------------------------------------------------------


class _Night(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return real_datetime(2026, 10, 31, 23, 30, tzinfo=tz)


async def test_a_night_takeover_does_not_hold_up_the_reader(fake_bus, monkeypatch):
    """A camera takeover's brightness boost ran ddcutil (20 s timeout) inside
    the reader: the WebRTC answers for the takeover's own tiles queued behind
    it until their offers timed out."""
    monkeypatch.setattr(scheduler_module, "datetime", _Night)
    config = {
        "night": {"dim_at": "23:00", "wake_at": "07:00", "method": "ddc"},
        "ha": {"alerts": [{
            "entity": "binary_sensor.front_door", "state": "on", "takeover": True,
            "cameras": ["camera.porch"], "cooldown_seconds": 0,
        }]},
    }
    scheduler = NightScheduler(fake_bus, lambda: config)
    wedged = asyncio.Event()  # an i2c bus that doesn't answer
    levels = []

    async def ddcutil(level):
        levels.append(level)
        await wedged.wait()
        return True

    monkeypatch.setattr(scheduler, "_ddcutil", ddcutil)
    bridge = HABridge(fake_bus, lambda: config)
    bridge.camera_alerts = CameraAlertHub(fake_bus, lambda: config, scheduler=scheduler)
    event = {
        "event_type": "state_changed",
        "data": {
            "entity_id": "binary_sensor.front_door",
            "old_state": {"state": "off"},
            "new_state": {"state": "on", "attributes": {}},
        },
    }
    await asyncio.wait_for(bridge._on_event(event), timeout=1)
    assert len(fake_bus.of_type("camera_alert")) == 1
    await until(lambda: levels == [100])  # the boost is under way, in its own task
    wedged.set()
    await scheduler._end_boost()


async def test_large_state_dumps_fit_in_a_frame(fake_bus, monkeypatch):
    # get_states is one frame; websockets' default 1 MiB cap closed the
    # connection (1009) on a big install, and the bridge reconnected forever.
    seen = {}

    def connect(url, **kwargs):
        seen.update(kwargs)
        raise OSError("refused")

    monkeypatch.setattr(ha_bridge.websockets, "connect", connect)
    bridge = HABridge(fake_bus, lambda: {})
    with pytest.raises(OSError):
        await bridge._session("ws://ha.invalid/api/websocket")
    assert seen["max_size"] >= 16 * 1024 * 1024
