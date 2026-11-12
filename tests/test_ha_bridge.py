"""backend/ha_bridge.py: the service-call allowlist and state fan-out."""
from __future__ import annotations

import asyncio
import json
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


def removed(entity: str) -> dict:
    """What HA sends when an entity is deleted, or renamed away from this id."""
    return {
        "event_type": "state_changed",
        "data": {"entity_id": entity, "old_state": {"state": "on"}, "new_state": None},
    }


async def test_an_entity_removed_from_ha_is_forgotten(bridge, fake_bus):
    # new_state: null was ignored, so the entity stayed in the admin's entity
    # list and on the display until the bridge next reconnected.
    on = {"state": "on", "attributes": {}}
    bridge.all_states = {"light.desk": on, "fan.bedroom": on, "lock.front_door": on}

    await bridge._on_event(removed("lock.front_door"))  # not mapped: nothing to tell displays
    assert set(bridge.all_states) == {"light.desk", "fan.bedroom"}
    assert fake_bus.messages == []

    await bridge._on_event(removed("light.desk"))
    assert [e["entity_id"] for e in bridge.list_entities()] == ["fan.bedroom"]
    # the mapped set again: `ha_state` has no way to say "gone"
    assert fake_bus.messages == [
        {"type": "ha_states", "status": bridge.status, "states": {"fan.bedroom": on}}
    ]

    await bridge._on_event(removed("light.desk"))  # already gone
    await bridge._on_event(removed("sensor.never_seen"))
    assert len(fake_bus.messages) == 1


def test_list_entities_sorted_by_domain_then_name(bridge):
    bridge.all_states = {
        "light.b": {"state": "on", "attributes": {"friendly_name": "beta"}},
        "fan.x": {"state": "off", "attributes": {}},
        "light.a": {"state": "off", "attributes": {"friendly_name": "Alpha"}},
    }
    assert [e["entity_id"] for e in bridge.list_entities()] == ["fan.x", "light.a", "light.b"]


# ---- connection robustness -----------------------------------------------------------


class FakeHA:
    """Home Assistant's end of the bridge's WebSocket; the test feeds the frames."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()

    def push(self, message: dict) -> None:
        self.inbox.put_nowait(json.dumps(message))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def recv(self) -> str:
        return await self.inbox.get()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self.recv()


async def test_no_command_goes_out_before_auth_has_succeeded(fake_bus, monkeypatch):
    # The socket was published as soon as it opened, so a tile tap landing
    # mid-handshake was sent ahead of the auth message.
    ha = FakeHA()
    monkeypatch.setattr(ha_bridge.websockets, "connect", lambda url, **kwargs: ha)
    bridge = HABridge(fake_bus, lambda: HA_CONFIG)
    bridge.token = "secret"

    async def refused() -> None:
        async def on_event(event):
            pass

        for call in (
            bridge.call_service("light", "toggle", "light.desk"),
            bridge.send_command({"type": "camera/webrtc/candidate"}),
            bridge.subscribe({"type": "camera/webrtc/offer"}, on_event),
        ):
            with pytest.raises(RuntimeError, match="not connected"):
                await asyncio.wait_for(call, timeout=1)

    session = asyncio.create_task(bridge._session("ws://ha.test/api/websocket"))
    try:
        await asyncio.sleep(0.01)  # connected; HA has not said auth_required yet
        await refused()
        assert ha.sent == []

        ha.push({"type": "auth_required"})
        await until(lambda: ha.sent)
        await refused()  # auth sent, not yet accepted
        assert ha.sent == [{"type": "auth", "access_token": "secret"}]
        assert bridge.status == "unconfigured"  # no env in tests; not "connected" either

        ha.push({"type": "auth_ok"})
        await until(lambda: len(ha.sent) == 2)
        assert ha.sent[1] == {"type": "get_states", "id": 1}
        assert bridge.status == "connected"
        ha.push({"type": "result", "id": 1, "success": True, "result": [
            {"entity_id": "light.desk", "state": "on", "attributes": {}},
        ]})
        await until(lambda: len(ha.sent) == 3)
        ha.push({"type": "result", "id": 2, "success": True, "result": None})
        await until(lambda: fake_bus.of_type("ha_states"))

        call = asyncio.create_task(bridge.call_service("light", "toggle", "light.desk"))
        await until(lambda: len(ha.sent) == 4)
        assert ha.sent[3]["type"] == "call_service" and ha.sent[3]["id"] == 3
        ha.push({"type": "result", "id": 3, "success": True, "result": {"ok": True}})
        assert await asyncio.wait_for(call, timeout=1) == {"ok": True}

        # and a removal arriving over the socket reaches displays
        ha.push({"type": "event", "id": 2, "event": removed("light.desk")})
        await until(lambda: len(fake_bus.of_type("ha_states")) == 2)
        assert fake_bus.of_type("ha_states")[-1]["states"] == {}
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
    with pytest.raises(RuntimeError, match="not connected"):
        await bridge.call_service("light", "toggle", "light.desk")


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
