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


# ---- frame heartbeat: is the kiosk still drawing? ---------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ws_module, "_now", clock)
    monkeypatch.setattr(ws_module, "_kiosks", set())
    monkeypatch.setattr(ws_module, "display_clients", 0)
    monkeypatch.setattr(ws_module, "kiosk_clients", 0)
    return clock


async def connect(host: str = "127.0.0.1"):
    sock = FakeSocket(make_app(), host=host)
    task = asyncio.create_task(ws_module.ws_display(sock))
    await until(lambda: sock.sent)

    async def ping(**fields) -> None:
        seen = sock.sent.count({"type": "pong"})
        await sock.incoming.put({"type": "ping", **fields})
        await until(lambda: sock.sent.count({"type": "pong"}) > seen)

    async def leave() -> None:
        await sock.incoming.put(None)
        await asyncio.wait_for(task, timeout=1)

    return ping, leave


async def test_frame_age_is_the_time_since_the_kiosks_frame_count_moved(clock):
    # The compositor's frame loop can die under a live page: the socket stays
    # up and the pings keep coming, but the frame count in them stands still.
    ping, leave = await connect()
    assert ws_module.kiosk_liveness() == {
        "connected_for": 0.0, "ping_age": None, "frame_age": None, "blanked": False,
    }
    await ping(frames=3)
    clock.now += 10
    await ping(frames=4)
    clock.now += 10
    await ping(frames=4)
    clock.now += 10
    await ping(frames=4, blanked=True)
    clock.now += 2
    assert ws_module.kiosk_liveness() == {
        "connected_for": 32.0, "ping_age": 2.0, "frame_age": 22.0, "blanked": True,
    }
    await leave()
    assert ws_module.kiosk_liveness() is None


async def test_a_kiosk_already_frozen_when_it_connects_is_caught(clock):
    # After a backend restart the frozen page reconnects and reports the same
    # count for ever: the first report has to start the clock.
    ping, leave = await connect()
    await ping(frames=57)
    clock.now += 300
    await ping(frames=57)
    assert ws_module.kiosk_liveness()["frame_age"] == 300.0
    await leave()


async def test_a_bundle_from_before_the_heartbeat_only_has_a_ping_age(clock):
    ping, leave = await connect()
    await ping()
    clock.now += 15
    assert ws_module.kiosk_liveness() == {
        "connected_for": 15.0, "ping_age": 15.0, "frame_age": None, "blanked": False,
    }
    await ping(frames="12")  # not a count
    await ping(frames=True)
    assert ws_module.kiosk_liveness()["frame_age"] is None
    await leave()


async def test_only_sockets_from_this_machine_are_the_kiosk(clock):
    ping, leave = await connect(host="192.168.1.50")  # a laptop's /display tab
    await ping(frames=1)
    assert ws_module.kiosk_liveness() is None
    await leave()


async def test_a_hidden_tab_is_not_a_frozen_panel(clock):
    # A background tab reached through an SSH tunnel gets no animation frames
    # by design; the kiosk beside it is the one that is judged.
    tab_ping, tab_leave = await connect()
    kiosk_ping, kiosk_leave = await connect()
    await tab_ping(frames=0, hidden=True)
    await kiosk_ping(frames=5)
    clock.now += 600
    await tab_ping(frames=0, hidden=True)
    await kiosk_ping(frames=65)
    clock.now += 1
    assert ws_module.kiosk_liveness()["frame_age"] == 1.0
    await kiosk_leave()
    assert ws_module.kiosk_liveness() is None  # nothing left to judge
    await tab_leave()


async def test_of_two_visible_pages_the_one_still_drawing_is_reported(clock):
    first_ping, first_leave = await connect()
    second_ping, second_leave = await connect()
    await first_ping(frames=1)
    await second_ping(frames=1)
    clock.now += 120
    await second_ping(frames=2)
    assert ws_module.kiosk_liveness()["frame_age"] == 0.0
    await first_leave()
    await second_leave()


async def test_a_socket_that_left_stops_counting_before_its_teardown(clock):
    # Closing its camera sessions waits on Home Assistant, 10 s each when HA
    # is unresponsive: a dead socket counted as a panel for over a minute.
    sock = FakeSocket(make_app())
    counts_during_teardown = []

    async def slow_close(owner) -> None:
        counts_during_teardown.append((ws_module.display_clients, ws_module.kiosk_clients))

    sock.app.state.webrtc.close_owner = slow_close
    task = asyncio.create_task(ws_module.ws_display(sock))
    await until(lambda: sock.sent)
    assert (ws_module.display_clients, ws_module.kiosk_clients) == (1, 1)
    await sock.incoming.put(None)
    await asyncio.wait_for(task, timeout=1)
    assert counts_during_teardown == [(0, 0)]
    assert (ws_module.display_clients, ws_module.kiosk_clients) == (0, 0)
