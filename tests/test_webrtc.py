"""WebRTC camera video: the HA bridge's subscriptions and the signalling relay."""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.camera_alert import camera_token
from backend.ha_bridge import HABridge
from backend.webrtc import MAX_SESSIONS, WebRTCRelay
from helpers import FakeBus, until

DOORBELL = "camera.g4_doorbell_pro_high_resolution_channel"
GARAGE = "camera.garage_driveway_high_resolution_channel"
CONFIG = {"ha": {"alerts": [{"entity": "binary_sensor.door", "state": "on", "cameras": [DOORBELL, GARAGE]}]}}


# ---- bridge subscriptions ------------------------------------------------------------


class FakeWs:
    """Enough of a websockets connection for HABridge._reader/_command."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)


@pytest.fixture
def bridge():
    b = HABridge(FakeBus(), lambda: {})
    b._ws = FakeWs()
    return b


async def test_subscription_events_route_by_id(bridge):
    ws = bridge._ws
    reader = asyncio.create_task(bridge._reader(ws))
    events: list[dict] = []

    async def on_event(event):
        events.append(event)

    sub = asyncio.create_task(bridge.subscribe({"type": "camera/webrtc/offer", "entity_id": DOORBELL, "offer": "v=0"}, on_event))
    await until(lambda: ws.sent)
    sub_id = ws.sent[0]["id"]
    assert ws.sent[0]["type"] == "camera/webrtc/offer"
    # HA can emit the first event right behind the result — the callback is
    # registered before the command goes out.
    await ws.incoming.put({"id": sub_id, "type": "result", "success": True, "result": None})
    await ws.incoming.put({"id": sub_id, "type": "event", "event": {"type": "session", "session_id": "s1"}})
    await ws.incoming.put({"id": sub_id, "type": "event", "event": {"type": "answer", "answer": "v=0 answer"}})
    assert await sub == sub_id
    await until(lambda: len(events) == 2)
    assert [e["type"] for e in events] == ["session", "answer"]  # in order

    # Unclaimed events still go to the state_changed handler.
    seen = []

    async def on_state(event):
        seen.append(event)

    bridge._on_event = on_state
    await ws.incoming.put({"id": 999, "type": "event", "event": {"event_type": "state_changed"}})
    await until(lambda: seen)

    unsub = asyncio.create_task(bridge.unsubscribe(sub_id))
    await until(lambda: len(ws.sent) == 2)
    assert ws.sent[1]["type"] == "unsubscribe_events" and ws.sent[1]["subscription"] == sub_id
    await ws.incoming.put({"id": ws.sent[1]["id"], "type": "result", "success": True, "result": None})
    await unsub
    await ws.incoming.put({"id": sub_id, "type": "event", "event": {"type": "candidate"}})
    await until(lambda: len(seen) == 2)  # no longer claimed by the subscription
    await ws.incoming.put(None)
    await reader


async def test_failed_subscribe_is_not_left_registered(bridge):
    ws = bridge._ws
    reader = asyncio.create_task(bridge._reader(ws))

    async def on_event(event):
        pass

    sub = asyncio.create_task(bridge.subscribe({"type": "camera/webrtc/offer"}, on_event))
    await until(lambda: ws.sent)
    await ws.incoming.put({"id": ws.sent[0]["id"], "type": "result", "success": False, "error": {"code": "not_supported"}})
    with pytest.raises(RuntimeError):
        await sub
    assert bridge._subscriptions == {}
    await ws.incoming.put(None)
    await reader


# ---- relay ----------------------------------------------------------------------------


class FakeBridge:
    def __init__(self, states=None, fail=None) -> None:
        self.subscribed: list[tuple[int, dict]] = []
        self.callbacks: dict[int, object] = {}
        self.commands: list[dict] = []
        self.unsubscribed: list[int] = []
        self.all_states = states or {}
        self.fail = fail
        self._next = 0

    async def subscribe(self, payload, on_event):
        if self.fail:
            raise RuntimeError(self.fail)
        self._next += 1
        self.subscribed.append((self._next, payload))
        self.callbacks[self._next] = on_event
        return self._next

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)

    async def send_command(self, payload):
        self.commands.append(payload)


def relay_with(bridge):
    return WebRTCRelay(bridge, lambda: CONFIG)


async def test_offer_relays_answer_and_flushes_early_candidates():
    bridge = FakeBridge()
    relay = relay_with(bridge)
    owner, sent = object(), []
    await relay.offer(owner, "r1", camera_token(DOORBELL), "v=0 offer", sent.append)
    [(sub_id, payload)] = bridge.subscribed
    assert payload == {"type": "camera/webrtc/offer", "entity_id": DOORBELL, "offer": "v=0 offer"}
    # A display candidate before HA named the session is held, then flushed.
    await relay.candidate(owner, "r1", {"candidate": "a=candidate:1"})
    assert bridge.commands == []
    await bridge.callbacks[sub_id]({"type": "session", "session_id": "S"})
    await bridge.callbacks[sub_id]({"type": "answer", "answer": "v=0 answer"})
    assert bridge.commands == [{
        "type": "camera/webrtc/candidate", "entity_id": DOORBELL, "session_id": "S",
        "candidate": {"candidate": "a=candidate:1"},
    }]
    assert [m["event"]["type"] for m in sent] == ["session", "answer"]
    assert all(m["type"] == "webrtc" and m["request"] == "r1" for m in sent)
    assert relay.active == 1
    await relay.close(owner, "r1")
    assert bridge.unsubscribed == [sub_id] and relay.active == 0


async def test_only_allowlisted_cameras():
    bridge = FakeBridge()
    relay = relay_with(bridge)
    sent = []
    await relay.offer(object(), "r1", camera_token("camera.baby_room_high_resolution_channel"), "v=0", sent.append)
    await relay.offer(object(), "r2", DOORBELL, "v=0", sent.append)  # raw entity ids aren't tokens
    assert bridge.subscribed == []
    assert [m["event"] for m in sent] == [{"type": "error", "message": "unknown camera"}] * 2


async def test_wall_prefers_an_available_medium_channel():
    medium = DOORBELL.replace("_high_", "_medium_")
    bridge = FakeBridge(states={medium: {"state": "recording"}})
    relay = relay_with(bridge)
    await relay.offer(object(), "a", camera_token(DOORBELL), "v=0", lambda m: None, wall=True)
    await relay.offer(object(), "b", camera_token(DOORBELL), "v=0", lambda m: None, wall=False)
    await relay.offer(object(), "c", camera_token(GARAGE), "v=0", lambda m: None, wall=True)  # no sibling
    assert [p["entity_id"] for _, p in bridge.subscribed] == [medium, DOORBELL, GARAGE]
    bridge.all_states[medium] = {"state": "unavailable"}
    await relay.offer(object(), "d", camera_token(DOORBELL), "v=0", lambda m: None, wall=True)
    assert bridge.subscribed[-1][1]["entity_id"] == DOORBELL


async def test_disconnect_closes_only_that_displays_sessions():
    bridge = FakeBridge()
    relay = relay_with(bridge)
    a, b = object(), object()
    await relay.offer(a, "r1", camera_token(DOORBELL), "v=0", lambda m: None)
    await relay.offer(a, "r2", camera_token(GARAGE), "v=0", lambda m: None)
    await relay.offer(b, "r1", camera_token(DOORBELL), "v=0", lambda m: None)
    await relay.close_owner(a)
    assert sorted(bridge.unsubscribed) == [1, 2] and relay.active == 1


async def test_failures_reach_the_display():
    sent = []
    relay = relay_with(FakeBridge(fail="Home Assistant is not connected"))
    await relay.offer(object(), "r1", camera_token(DOORBELL), "v=0", sent.append)
    assert sent[0]["event"] == {"type": "error", "message": "Home Assistant is not connected"}
    assert relay.active == 0

    bridge = FakeBridge()
    relay = relay_with(bridge)
    for i in range(MAX_SESSIONS):
        await relay.offer(object(), f"r{i}", camera_token(DOORBELL), "v=0", lambda m: None)
    sent = []
    await relay.offer(object(), "extra", camera_token(DOORBELL), "v=0", sent.append)
    assert sent[0]["event"]["message"] == "too many sessions"
