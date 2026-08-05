"""Remote control of the display from the admin GUI (or curl)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..camera_alert import MAX_CAMERAS, first_takeover_alert
from ..collectors.sports import build_test_event
from ..collectors.weather_alerts import TEST_ALERT

router = APIRouter()

ACTIONS = {
    "next", "prev", "pin", "blank", "wake", "reload",
    "celebrate_test", "weather_alert_test", "camera_alert_test",
    # Handled entirely by the display: fabricated Starship flight-day preview.
    "starship_test",
}


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
    if action == "weather_alert_test":
        # Canned (not live-fetched): there may be no active NWS alert to replay.
        await request.app.state.bus.broadcast({"type": "weather_alert", "alert": TEST_ALERT})
        return {"ok": True, "alert": TEST_ALERT}
    if action == "camera_alert_test":
        # Replays the first configured takeover alert so the test exercises the
        # real thing; failing that, any cameras picked in the HA tab, so the
        # button is useful before a door is wired up. cooldown_seconds=0 — a
        # test must never be eaten by the flap guard.
        config = request.app.state.config
        alert = first_takeover_alert(config)
        if alert is not None:
            fields = dict(
                title=alert.get("takeover_title") or alert.get("text") or "Camera test",
                subtitle=alert.get("entity"),
                cameras=alert.get("cameras") or [],
                severity=alert.get("severity") or "alert",
                duration_seconds=alert.get("duration_seconds") or 30,
                transport=alert.get("transport") or "stream",
            )
        else:
            cameras = ((config or {}).get("ha") or {}).get("cameras") or []
            if not cameras:
                return JSONResponse(
                    {
                        "error": "no cameras configured — pick cameras (and optionally "
                        "a door alert to trigger them) in the Home Assistant tab"
                    },
                    status_code=400,
                )
            fields = dict(
                title="Camera test",
                subtitle=None,
                cameras=cameras[:MAX_CAMERAS],
                severity="alert",
                duration_seconds=20,
                transport="stream",
            )
        event = await request.app.state.camera_alerts.fire(
            key="test", source="test", cooldown_seconds=0, **fields
        )
        if event is None:
            return JSONResponse(
                {"error": "no usable cameras (entity ids must start with 'camera.')"},
                status_code=400,
            )
        return {"ok": True, "event": event}
    await request.app.state.bus.broadcast({"type": "control", "action": action})
    return {"ok": True}
