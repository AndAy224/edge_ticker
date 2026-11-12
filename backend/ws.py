"""WebSocket endpoints: /ws/display (the panel) and /ws/admin (live preview)."""
from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import time
from collections.abc import Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import db
from .build import builds
from .origin import cross_site
from .sysinfo import lan_ip
from .tasks import Lane, spawn

log = logging.getLogger(__name__)
router = APIRouter()


async def _serve(
    websocket: WebSocket,
    liveness: Liveness | None = None,
    gone: Callable[[], None] = lambda: None,
) -> None:
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
                await _handle(app, queue, message, work, liveness)
        finally:
            # Before the teardown below: closing this client's camera sessions
            # waits on Home Assistant (10 s each when it is unresponsive), and
            # a socket that has left must not count as a panel meanwhile.
            gone()
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


async def _handle(
    app, queue: asyncio.Queue, message: dict, work: _Work, liveness: Liveness | None = None
) -> None:
    # Replies go through this connection's queue so only the sender task
    # ever writes to the socket.
    kind = message.get("type")
    if kind == "ping":
        if liveness is not None:
            liveness.ping(message)
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

_now = time.monotonic  # patchable


class Liveness:
    """What one display socket from this machine says about itself in its
    pings (every 10 s).

    A connected socket does not prove a working panel: with the compositor's
    frame loop dead the page lives on — timers fire, the socket stays up,
    module fetches continue — while the screen shows one frozen picture, for
    hours, until someone power-cycles the box. So each ping carries `frames`,
    a count that only advances when the page gets an animation frame, and the
    time it last moved is what /health reports as `frame_age`."""

    def __init__(self) -> None:
        self.connected_at = _now()
        self.pinged_at: float | None = None
        self.frames: int | None = None
        self.frames_at: float | None = None
        self.hidden = False
        self.blanked = False

    def ping(self, message: dict) -> None:
        now = _now()
        self.pinged_at = now
        self.hidden = message.get("hidden") is True
        self.blanked = message.get("blanked") is True
        frames = message.get("frames")
        if isinstance(frames, bool) or not isinstance(frames, int):
            return  # a bundle from before the heartbeat: pings only
        # The first report starts the clock too — a panel already frozen when
        # it (re)connects, after a backend restart say, never moves off it.
        if frames != self.frames:
            self.frames = frames
            self.frames_at = now

    def report(self) -> dict:
        now = _now()

        def age(since: float | None) -> float | None:
            return None if since is None else round(now - since, 1)

        return {
            "connected_for": round(now - self.connected_at, 1),
            "ping_age": age(self.pinged_at),
            "frame_age": age(self.frames_at),
            "blanked": self.blanked,
        }


_kiosks: set[Liveness] = set()


def kiosk_liveness() -> dict | None:
    """The watchdog's view of the panel: the freshest display socket from this
    machine. A hidden page gets no animation frames by design (a background
    tab through an SSH tunnel), so it is not judged; and of several, the one
    whose frames moved last is the one somebody can see."""
    reports = [k.report() for k in _kiosks if not k.hidden]
    if not reports:
        return None

    def staleness(report: dict) -> tuple[bool, float]:
        frame_age = report["frame_age"]
        return (frame_age is None, frame_age or 0.0)

    return min(reports, key=staleness)


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
    liveness = Liveness() if _loopback(websocket) else None
    display_clients += 1
    if liveness is not None:
        kiosk_clients += 1
        _kiosks.add(liveness)
    counted = True

    def gone() -> None:
        global display_clients, kiosk_clients
        nonlocal counted
        if not counted:
            return
        counted = False
        display_clients -= 1
        if liveness is not None:
            kiosk_clients -= 1
            _kiosks.discard(liveness)

    try:
        await _serve(websocket, liveness, gone)
    finally:
        gone()


@router.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket) -> None:
    await _serve(websocket)
