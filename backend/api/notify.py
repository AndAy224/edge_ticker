"""Push a message to the panel from anything on the LAN — a Home Assistant
automation (through a `rest_command`), a script, curl.

    POST /api/notify {"title": "Washer done"}
    POST /api/notify {"title": "Garage open", "message": "for 10 minutes",
                      "kind": "takeover", "cameras": ["camera.garage"]}

`kind: "toast"` (the default) pops the display's toast for `duration_seconds`;
a blanked panel drops it, like every other toast. `kind: "takeover"` goes
through the camera-alert path (`CameraAlertHub.fire`), so it gets that path's
wake, dim lift, cooldown, extend-on-refire and priority rules for free: with
`cameras` it is a camera takeover (only cameras the HA tab already allows),
without them the message fills the panel where the wall would be.

No login, like every write here: LAN trust, plus the cross-site guard in
backend/origin.py (HA's rest_command sends no Origin header, so it passes).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..camera_alert import MAX_CAMERAS, SEVERITIES, _camera_entity, allowed_cameras

router = APIRouter()

KINDS = {"toast", "takeover"}
MAX_TITLE, MAX_MESSAGE = 120, 300
TOAST_SECONDS, TOAST_MIN, TOAST_MAX = 8, 3, 60
TAKEOVER_SECONDS = 20  # fire() clamps to its own 5–120
DEFAULT_COOLDOWN = 5.0  # a looping automation shouldn't strobe the panel


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_notice(body: Any, config: dict) -> tuple[dict | None, list[str]]:
    """The validated notice, or None and what is wrong with the request."""
    if not isinstance(body, dict):
        return None, ["body must be a JSON object"]
    errors: list[str] = []

    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title is required (a non-empty string)")
        title = ""
    message = body.get("message")
    if message is not None and not isinstance(message, str):
        errors.append("message must be a string")
        message = None

    kind = body.get("kind", "toast")
    if kind not in KINDS:
        errors.append(f"kind must be one of {sorted(KINDS)}")
    severity = body.get("severity", "info" if kind == "toast" else "alert")
    if severity not in SEVERITIES:
        errors.append(f"severity must be one of {sorted(SEVERITIES)}")
    duration = body.get("duration_seconds")
    if duration is not None and not _number(duration):
        errors.append("duration_seconds must be a number")
        duration = None
    wake = body.get("wake", True)
    if not isinstance(wake, bool):
        errors.append("wake must be true or false")
    key = body.get("key")
    if key is not None and not isinstance(key, str):
        errors.append("key must be a string")
    cooldown = body.get("cooldown_seconds", DEFAULT_COOLDOWN)
    if not _number(cooldown) or cooldown < 0:
        errors.append("cooldown_seconds must be a number ≥ 0")

    cameras = body.get("cameras") or []
    if not isinstance(cameras, list):
        errors.append("cameras must be a list of camera entity ids")
        cameras = []
    elif cameras and kind != "takeover":
        errors.append('cameras only apply to kind "takeover"')
    else:
        allowed = set(allowed_cameras(config).values())
        refused = [c for c in cameras if _camera_entity(c) not in allowed]
        if refused:
            # Only cameras already picked in the HA tab are proxyable; naming
            # them here would just produce dead tiles.
            errors.append(f"cameras not allowed in the HA tab: {refused}")
        elif len(cameras) > MAX_CAMERAS:
            errors.append(f"at most {MAX_CAMERAS} cameras")

    if errors:
        return None, errors
    title = title.strip()[:MAX_TITLE]
    message = (message or "").strip()[:MAX_MESSAGE] or None
    if kind == "toast":
        seconds = max(TOAST_MIN, min(TOAST_MAX, duration or TOAST_SECONDS))
    else:
        seconds = duration or TAKEOVER_SECONDS
    return {
        "kind": kind,
        "title": title,
        "message": message,
        "severity": severity,
        "duration_seconds": seconds,
        "wake": wake,
        "key": key or f"notify:{title.lower()}",
        "cooldown_seconds": float(cooldown),
        "cameras": cameras,
    }, []


@router.post("/notify")
async def notify(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    notice, errors = parse_notice(body, request.app.state.config)
    if notice is None:
        return JSONResponse({"error": "; ".join(errors), "errors": errors}, status_code=400)

    if notice["kind"] == "toast":
        await request.app.state.bus.broadcast(
            {
                "type": "notify",
                "notice": {
                    k: notice[k] for k in ("title", "message", "severity", "duration_seconds")
                },
            }
        )
        return {"ok": True, "kind": "toast"}

    cameras = notice["cameras"]
    event = await request.app.state.camera_alerts.fire(
        key=notice["key"],
        source="notify",
        title=notice["title"],
        # with cameras the message is the banner's subtitle; without, the body
        subtitle=notice["message"] if cameras else None,
        message=None if cameras else (notice["message"] or notice["title"]),
        cameras=cameras,
        severity=notice["severity"],
        duration_seconds=notice["duration_seconds"],
        wake=notice["wake"],
        cooldown_seconds=notice["cooldown_seconds"],
    )
    if event is None:
        return {"ok": False, "kind": "takeover", "suppressed": "cooldown"}
    return {"ok": True, "kind": "takeover", "event": event}
