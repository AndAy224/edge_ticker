"""Remote control of the display from the admin GUI (or curl)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

ACTIONS = {"next", "prev", "pin", "blank", "wake", "reload"}


@router.post("/control")
async def control(request: Request):
    body = await request.json()
    action = body.get("action") if isinstance(body, dict) else None
    if action not in ACTIONS:
        return JSONResponse(
            {"error": f"action must be one of {sorted(ACTIONS)}"}, status_code=400
        )
    await request.app.state.bus.broadcast({"type": "control", "action": action})
    return {"ok": True}
