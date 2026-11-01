"""GET/PUT the full config document. PUT validates, persists, converges the
collectors on the new config, and pushes it to every display/admin client.
Every save is kept (last 20) so a bad one can be rolled back from the admin.

Saves are optimistic-concurrency checked: GET returns the document's version
as an ETag, and a PUT carrying If-Match is refused with 409 (and the current
config) when the stored document has moved on. The admin always sends it; a
PUT without it (curl, the layout audit) overwrites as before."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import db
from ..config_check import changed_paths, validate

router = APIRouter()


def _etag(config) -> str:
    return f'"{db.config_version(config)}"'


def _lock(app) -> asyncio.Lock:
    # Check-then-save has to be atomic, or two tabs saving from the same version
    # would both pass the If-Match check.
    lock = getattr(app.state, "config_lock", None)
    if lock is None:
        lock = app.state.config_lock = asyncio.Lock()
    return lock


@router.get("/config")
async def get_config(request: Request, response: Response) -> dict:
    response.headers["ETag"] = _etag(request.app.state.config)
    return request.app.state.config


@router.put("/config")
async def put_config(request: Request):
    body = await request.json()
    return await _apply(request, body, if_match=request.headers.get("if-match"))


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


async def _apply(request: Request, config, if_match: str | None = None):
    async with _lock(request.app):
        current = request.app.state.config
        if if_match is not None and if_match.strip() != "*":
            if if_match.strip().removeprefix("W/").strip('"') != db.config_version(current):
                return JSONResponse(
                    {
                        "error": "the config changed since this copy was loaded "
                        "(another tab saved, or a version was restored)",
                        "version": db.config_version(current),
                        "config": current,
                    },
                    status_code=409,
                    headers={"ETag": _etag(current)},
                )
        if isinstance(config, dict):
            config = db.with_defaults(config)
        errors = validate(config)
        if errors:
            # Nothing is saved: a config that can't build its collectors used to
            # be persisted first, then crash-loop the next boot.
            return JSONResponse({"error": "; ".join(errors), "errors": errors}, status_code=400)

        await db.put_config(config)
        request.app.state.config = config
        version = db.config_version(config)

        bus = request.app.state.bus
        restarted = await request.app.state.manager.apply(bus, config)
        await bus.broadcast({"type": "config", "config": config, "version": version})
        # HA mapping may have changed — resend the mapped entity states.
        bridge = request.app.state.ha
        await bus.broadcast(
            {"type": "ha_states", "status": bridge.status, "states": bridge.mapped_states()}
        )
        # The stored document (defaults merged in) and its version, so the
        # admin's next save is checked against what it actually saved.
        return JSONResponse(
            {"ok": True, "restarted": restarted, "version": version, "config": config},
            headers={"ETag": f'"{version}"'},
        )
