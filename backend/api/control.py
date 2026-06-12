"""Remote control of the display from the admin GUI (or curl)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..collectors.sports import build_test_event

router = APIRouter()

ACTIONS = {"next", "prev", "pin", "blank", "wake", "reload", "celebrate_test"}


@router.post("/control")
async def control(request: Request):
    body = await request.json()
    action = body.get("action") if isinstance(body, dict) else None
    if action not in ACTIONS:
        return JSONResponse(
            {"error": f"action must be one of {sorted(ACTIONS)}"}, status_code=400
        )
    if action == "celebrate_test":
        # Real data: the last touchdown from the latest Packers game of last
        # season. Built once per process, then cached.
        event = getattr(request.app.state, "celebrate_test_event", None)
        if event is None:
            try:
                event = await build_test_event()
            except Exception as exc:
                return JSONResponse(
                    {"error": f"could not build test event: {exc}"}, status_code=502
                )
            request.app.state.celebrate_test_event = event
        await request.app.state.bus.broadcast({"type": "sport_event", "event": event})
        return {"ok": True, "event": event}
    await request.app.state.bus.broadcast({"type": "control", "action": action})
    return {"ok": True}
