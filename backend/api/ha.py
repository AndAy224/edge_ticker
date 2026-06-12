"""HA helpers for the admin GUI: entity listing for the mapping tab, and a
REST path for service calls (the display normally uses /ws/display)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/ha/entities")
async def entities(request: Request) -> dict:
    bridge = request.app.state.ha
    return {"status": bridge.status, "entities": bridge.list_entities()}


@router.post("/ha/action")
async def action(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    bridge = request.app.state.ha
    try:
        await bridge.call_service(
            body.get("domain"),
            body.get("service"),
            body.get("entity_id"),
            body.get("data"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return {"ok": True}
