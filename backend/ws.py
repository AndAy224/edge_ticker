"""WebSocket endpoints: /ws/display (the panel) and /ws/admin (live preview)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)
router = APIRouter()


async def _serve(websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    bus = app.state.bus
    bridge = app.state.ha
    queue = bus.subscribe()
    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "modules": bus.snapshot(),
                "config": app.state.config,
                "ha": {"status": bridge.status, "states": bridge.mapped_states()},
                "display_state": bus.display_state,
            }
        )

        async def sender() -> None:
            while True:
                await websocket.send_json(await queue.get())

        send_task = asyncio.create_task(sender())
        try:
            while True:
                message = await websocket.receive_json()
                await _handle(app, queue, message)
        finally:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)


async def _handle(app, queue: asyncio.Queue, message: dict) -> None:
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
    elif kind == "ha_action":
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


@router.websocket("/ws/display")
async def ws_display(websocket: WebSocket) -> None:
    await _serve(websocket)


@router.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket) -> None:
    await _serve(websocket)
