"""GET/PUT the full config document. PUT persists, restarts collectors, and
pushes the new config to every connected display/admin client."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import db

router = APIRouter()


@router.get("/config")
async def get_config(request: Request) -> dict:
    return request.app.state.config


@router.put("/config")
async def put_config(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "config must be a JSON object"}, status_code=400)

    await db.put_config(body)
    request.app.state.config = body

    bus = request.app.state.bus
    await request.app.state.manager.restart(bus, body)
    await bus.broadcast({"type": "config", "config": body})
    # HA mapping may have changed — resend the mapped entity states.
    bridge = request.app.state.ha
    await bus.broadcast(
        {"type": "ha_states", "status": bridge.status, "states": bridge.mapped_states()}
    )
    return {"ok": True}
