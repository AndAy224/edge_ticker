"""WebSocket endpoints: /ws/display (the panel) and /ws/admin (live preview)."""
from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import db
from .build import builds
from .origin import cross_site
from .sysinfo import lan_ip
from .tasks import Lane, spawn

log = logging.getLogger(__name__)
router = APIRouter()


async def _serve(websocket: WebSocket) -> None:
    if cross_site(websocket.headers.get("origin"), websocket.headers.get("host")):
        # Any page a LAN user visits could otherwise read the stream and send
        # ha_action / control messages.
        log.warning("refused cross-site websocket from %s", websocket.headers.get("origin"))
        await websocket.close(code=1008)
        return
    await websocket.accept()
    app = websocket.app
    bus = app.state.bus
    bridge = app.state.ha
    queue = bus.subscribe()
    work = _Work()
    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "modules": bus.snapshot(),
                "config": app.state.config,
                # what the admin sends back in If-Match (PUT /api/config)
                "config_version": db.config_version(app.state.config),
                "ha": {"status": bridge.status, "states": bridge.mapped_states()},
                "display_state": bus.display_state,
                # Current night state, so a display that (re)connects inside the
                # dim window — the 04:00 nightly reload lands in it — dims
                # without waiting for the next dim_at edge.
                "night": app.state.scheduler.state(),
                # Host facts, resolved per connect — not bus state.
                "system": {"ip": lan_ip()},
                # Built asset set per page: a client on an older bundle reloads.
                "build": builds(),
            }
        )

        async def sender() -> None:
            while True:
                await websocket.send_json(await queue.get())

        send_task = asyncio.create_task(sender(), name="ws-sender")
        send_task.add_done_callback(functools.partial(_sender_ended, websocket))
        try:
            while True:
                message = await websocket.receive_json()
                await _handle(app, queue, message, work)
        finally:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            await work.cancel()
            # Camera video sessions this client opened end with it.
            await app.state.webrtc.close_owner(queue)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)


def _sender_ended(websocket: WebSocket, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if isinstance(exc, WebSocketDisconnect):
        return  # the client left; the receive loop is finding out too
    # A sender that died (a broadcast send_json can't serialize, say) used to
    # leave a zombie: the receive loop went on reading, nothing reached the
    # client — not even its pongs — and the watchdog still counted a panel.
    # Closed, the display notices and reconnects.
    log.warning("websocket sender failed, closing the connection: %r", exc)
    spawn(_close(websocket), "ws-close")


async def _close(websocket: WebSocket) -> None:
    try:
        await websocket.close(code=1011)
    except Exception:
        pass  # already closing


class _Work:
    """A client's slow handlers — Home Assistant round trips — run beside its
    receive loop and are cancelled with the connection.

    Awaited in the loop, one camera offer waiting on HA (up to its 10 s
    timeout) held up everything the display sent behind it, pings and closes
    included."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task] = set()
        # Service calls keep their order: two thermostat taps must not reach
        # HA the other way round.
        self._service_calls = Lane("ws-ha-action")

    def spawn(self, coro, name: str) -> None:
        self._track(spawn(coro, name))

    def in_order(self, coro) -> None:
        self._track(self._service_calls.run(coro))

    def _track(self, task: asyncio.Task) -> None:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def cancel(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _handle(app, queue: asyncio.Queue, message: dict, work: _Work) -> None:
    # Replies go through this connection's queue so only the sender task
    # ever writes to the socket.
    kind = message.get("type")
    if kind == "ping":
        queue.put_nowait({"type": "pong"})
    elif kind == "control":
        await app.state.bus.broadcast({"type": "control", "action": message.get("action")})
    elif kind == "display_state":
        # Display reports what it's showing; admin clients render a live preview.
        state = message.get("state") or {}
        app.state.bus.display_state = state
        await app.state.bus.broadcast({"type": "display_state", "state": state})
    elif kind == "webrtc_offer":
        # The session is registered here, before the next message is read, so
        # the candidates and close behind the offer find it; HA's round trip
        # runs beside the loop.
        pending = app.state.webrtc.open(
            queue, message.get("request"), message.get("camera"), message.get("offer"),
            queue.put_nowait, wall=message.get("wall") is True,
        )
        if pending is not None:
            work.spawn(pending, "webrtc-offer")
    elif kind == "webrtc_candidate":
        work.spawn(
            app.state.webrtc.candidate(queue, message.get("request"), message.get("candidate")),
            "webrtc-candidate",
        )
    elif kind == "webrtc_close":
        work.spawn(app.state.webrtc.close(queue, message.get("request")), "webrtc-close")
    elif kind == "ha_action":
        work.in_order(_service_call(app, queue, message))


async def _service_call(app, queue: asyncio.Queue, message: dict) -> None:
    try:
        await app.state.ha.call_service(
            message.get("domain"),
            message.get("service"),
            message.get("entity_id"),
            message.get("data"),
        )
    except Exception as exc:
        log.warning("ha_action failed: %s", exc)
        queue.put_nowait({"type": "error", "error": str(exc)})


# Connected display-channel sockets. `kiosk_clients` counts only those from
# this machine — the kiosk loads http://127.0.0.1:8080/display — and is what
# the watchdog checks for a hung kiosk: a laptop tab left open on /display
# kept display_clients above 0 and hid a dead panel.
display_clients = 0
kiosk_clients = 0


def _loopback(websocket: WebSocket) -> bool:
    try:
        ip = ipaddress.ip_address(getattr(websocket.client, "host", None) or "")
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1 on a dual-stack bind
    return (mapped or ip).is_loopback


@router.websocket("/ws/display")
async def ws_display(websocket: WebSocket) -> None:
    global display_clients, kiosk_clients
    kiosk = _loopback(websocket)
    display_clients += 1
    kiosk_clients += kiosk
    try:
        await _serve(websocket)
    finally:
        display_clients -= 1
        kiosk_clients -= kiosk


@router.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket) -> None:
    await _serve(websocket)
