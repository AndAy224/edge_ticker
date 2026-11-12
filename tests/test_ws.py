"""backend/ws.py: a display socket's receive loop, sender and client counts."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from backend import ws as ws_module
from backend.camera_alert import camera_token
from backend.state import Bus
from backend.webrtc import WebRTCRelay
from helpers import until

DOORBELL = "camera.g4_doorbell_pro_high_resolution_channel"
CONFIG = {"ha": {"cameras": [DOORBELL]}}


class FakeSocket:
    """Enough of Starlette's WebSocket for _serve(). `None` in `incoming` is
    the client going away; send_json serializes like the real one."""

    def __init__(self, app, host: str = "127.0.0.1") -> None:
        self.app = app
        self.headers: dict = {}
        self.client = SimpleNamespace(host=host, port=50000)
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed: int | None = None

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.closed = code
        await self.incoming.put(None)  # the client acknowledges and leaves

    async def send_json(self, data) -> None:
        self.sent.append(json.loads(json.dumps(data)))

    async def receive_json(self):
        message = await self.incoming.get()
        if message is None:
            raise WebSocketDisconnect(1000)
        return message


class StuckBridge:
    """An HA bridge whose offers never get an answer."""

    status = "connected"

    def __init__(self) -> None:
        self.all_states: dict = {}
        self.offers: list[dict] = []
        self.abandoned = 0

    def mapped_states(self) -> dict:
        return {}

    async def subscribe(self, payload, on_event):
        self.offers.append(payload)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.abandoned += 1
            raise

    async def unsubscribe(self, sub_id) -> None:
        pass


@pytest.fixture(autouse=True)
def _host_facts(monkeypatch):
    monkeypatch.setattr(ws_module, "lan_ip", lambda: None)  # would open a UDP socket
    monkeypatch.setattr(ws_module, "builds", lambda: {})


def make_app(bridge=None):
    bridge = bridge or StuckBridge()
    state = SimpleNamespace(
        bus=Bus(),
        config=CONFIG,
        ha=bridge,
        scheduler=SimpleNamespace(state=lambda: {"mode": "wake", "level": 100, "software": False}),
        webrtc=WebRTCRelay(bridge, lambda: CONFIG),
    )
    return SimpleNamespace(state=state)


async def test_a_stuck_offer_does_not_stall_the_connection():
    # The receive loop awaited each offer's HA round trip: while HA (or a
    # reader stuck behind a brightness boost) sat on it, the display's pings
    # and closes went unread.
    app = make_app()
    sock = FakeSocket(app)
    serve = asyncio.create_task(ws_module._serve(sock))
    await until(lambda: sock.sent)
    await sock.incoming.put({
        "type": "webrtc_offer", "request": "r1", "camera": camera_token(DOORBELL), "offer": "v=0",
    })
    await sock.incoming.put({"type": "webrtc_candidate", "request": "r1", "candidate": {"candidate": "c1"}})
    await sock.incoming.put({"type": "ping"})
    await until(lambda: {"type": "pong"} in sock.sent)
    assert len(app.state.ha.offers) == 1
    # The session was registered before the round trip left, so the candidate
    # behind the offer found it.
    [session] = app.state.webrtc._sessions.values()
    assert session.early_candidates == [{"candidate": "c1"}]
    await sock.incoming.put(None)  # the display goes away
    await asyncio.wait_for(serve, timeout=1)
    assert app.state.ha.abandoned == 1  # the offer's round trip ended with the connection
    assert app.state.webrtc.active == 0


async def test_a_dead_sender_closes_the_connection():
    # A broadcast the sender couldn't serialize killed it silently: the
    # connection lived on, nothing reached the client, and the watchdog went
    # on counting a panel.
    app = make_app()
    sock = FakeSocket(app)
    serve = asyncio.create_task(ws_module._serve(sock))
    await until(lambda: sock.sent)
    await app.state.bus.broadcast({"type": "oops", "value": object()})
    await asyncio.wait_for(serve, timeout=1)
    assert sock.closed == 1011
    assert app.state.bus.subscriber_count == 0


async def test_service_calls_keep_their_order():
    calls = []

    class Bridge(StuckBridge):
        async def call_service(self, domain, service, entity_id=None, data=None):
            if not calls:
                await asyncio.sleep(0.05)  # the first call is the slow one
            calls.append(data["temperature"])

    app = make_app(Bridge())
    sock = FakeSocket(app)
    serve = asyncio.create_task(ws_module._serve(sock))
    await until(lambda: sock.sent)
    for temperature in (71, 72):
        await sock.incoming.put({
            "type": "ha_action", "domain": "climate", "service": "set_temperature",
            "entity_id": "climate.hall", "data": {"temperature": temperature},
        })
    await sock.incoming.put({"type": "ping"})
    await until(lambda: {"type": "pong"} in sock.sent)
    assert calls == []  # the ping didn't wait for the slow call
    await until(lambda: calls == [71, 72])
    await sock.incoming.put(None)
    await asyncio.wait_for(serve, timeout=1)


@pytest.mark.parametrize(
    "host,kiosk",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("192.168.1.50", False),
        ("testclient", False),
        (None, False),
    ],
)
async def test_kiosk_clients_count_only_this_machine(monkeypatch, host, kiosk):
    # A laptop tab left on /display kept display_clients above 0 and hid a
    # hung kiosk from the watchdog.
    monkeypatch.setattr(ws_module, "display_clients", 0)
    monkeypatch.setattr(ws_module, "kiosk_clients", 0)
    sock = FakeSocket(make_app(), host=host)
    display = asyncio.create_task(ws_module.ws_display(sock))
    await until(lambda: sock.sent)
    assert ws_module.display_clients == 1
    assert ws_module.kiosk_clients == int(kiosk)
    await sock.incoming.put(None)
    await asyncio.wait_for(display, timeout=1)
    assert (ws_module.display_clients, ws_module.kiosk_clients) == (0, 0)
