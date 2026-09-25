"""GET/PUT the full config document. PUT validates, persists, converges the
collectors on the new config, and pushes it to every display/admin client.
Every save is kept (last 20) so a bad one can be rolled back from the admin."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import db
from ..config_check import changed_paths, validate

router = APIRouter()


@router.get("/config")
async def get_config(request: Request) -> dict:
    return request.app.state.config


@router.put("/config")
async def put_config(request: Request):
    body = await request.json()
    return await _apply(request, body)


@router.get("/config/history")
async def config_history() -> dict:
    """Saved versions, newest first, each with what it changed vs the one before."""
    versions = await db.history()
    out = []
    for i, version in enumerate(versions):
        older = versions[i + 1]["config"] if i + 1 < len(versions) else None
        out.append(
            {
                "id": version["id"],
                "saved_at": version["saved_at"],
                "current": i == 0,
                "changed": changed_paths(older, version["config"]) if older else [],
            }
        )
    return {"versions": out}


@router.get("/config/history/{version_id}")
async def config_version(version_id: int):
    for version in await db.history():
        if version["id"] == version_id:
            return version["config"]
    return JSONResponse({"error": "no such version"}, status_code=404)


@router.post("/config/restore")
async def restore_config(request: Request):
    body = await request.json()
    version_id = body.get("id") if isinstance(body, dict) else None
    for version in await db.history():
        if version["id"] == version_id:
            # Goes through the same validation as any save, and is itself
            # recorded — so a restore can be undone the same way.
            return await _apply(request, version["config"])
    return JSONResponse({"error": "no such version"}, status_code=404)


async def _apply(request: Request, config):
    if isinstance(config, dict):
        config = db.with_defaults(config)
    errors = validate(config)
    if errors:
        # Nothing is saved: a config that can't build its collectors used to be
        # persisted first, then crash-loop the next boot.
        return JSONResponse({"error": "; ".join(errors), "errors": errors}, status_code=400)

    await db.put_config(config)
    request.app.state.config = config

    bus = request.app.state.bus
    restarted = await request.app.state.manager.apply(bus, config)
    await bus.broadcast({"type": "config", "config": config})
    # HA mapping may have changed — resend the mapped entity states.
    bridge = request.app.state.ha
    await bus.broadcast(
        {"type": "ha_states", "status": bridge.status, "states": bridge.mapped_states()}
    )
    return {"ok": True, "restarted": restarted}
