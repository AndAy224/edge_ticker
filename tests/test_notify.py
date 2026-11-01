"""POST /api/notify: toasts and takeovers pushed from Home Assistant or curl."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from backend.api.notify import parse_notice, router
from backend.camera_alert import CameraAlertHub, camera_token
from helpers import FakeBus

CONFIG = {"ha": {"cameras": ["camera.garage"], "alerts": []}}


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.bus = FakeBus()
    app.state.config = CONFIG
    app.state.camera_alerts = CameraAlertHub(app.state.bus, lambda: app.state.config)
    return app


async def post(app, body=None, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ticker") as client:
        return await client.post("/api/notify", json=body, **kwargs)


async def test_toast_is_broadcast_to_displays(app):
    r = await post(app, {"title": "Washer done", "message": "Load finished"})
    assert r.status_code == 200 and r.json() == {"ok": True, "kind": "toast"}
    assert app.state.bus.of_type("notify") == [
        {"type": "notify", "notice": {"title": "Washer done", "message": "Load finished",
                                      "severity": "info", "duration_seconds": 8}}
    ]


async def test_takeover_without_cameras_carries_the_message(app):
    r = await post(app, {"title": "Garage open", "message": "for 10 minutes", "kind": "takeover"})
    assert r.status_code == 200 and r.json()["ok"] is True
    (msg,) = app.state.bus.of_type("camera_alert")
    event = msg["event"]
    assert event["source"] == "notify" and event["cameras"] == []
    assert event["title"] == "Garage open" and event["message"] == "for 10 minutes"
    assert event["severity"] == "alert" and event["duration_seconds"] == 20


async def test_takeover_with_an_allowed_camera(app):
    r = await post(app, {"title": "Garage open", "message": "for 10 minutes", "kind": "takeover",
                   "cameras": ["camera.garage"], "severity": "critical"})
    event = r.json()["event"]
    assert event["cameras"] == [{"id": camera_token("camera.garage"), "label": "camera.garage"}]
    assert event["subtitle"] == "for 10 minutes" and "message" not in event
    assert event["severity"] == "critical"


async def test_cameras_the_ha_tab_does_not_allow_are_refused(app):
    r = await post(app, {"title": "x", "kind": "takeover", "cameras": ["camera.bedroom"]})
    assert r.status_code == 400
    assert "not allowed" in r.json()["error"]
    assert app.state.bus.messages == []


async def test_a_repeat_inside_the_cooldown_is_suppressed(app):
    body = {"title": "Doorbell", "kind": "takeover"}
    assert (await post(app, body)).json()["ok"] is True
    assert (await post(app, body)).json() == {"ok": False, "kind": "takeover", "suppressed": "cooldown"}
    assert len(app.state.bus.of_type("camera_alert")) == 1


@pytest.mark.parametrize(
    "body, error",
    [
        ({}, "title is required"),
        ({"title": "  "}, "title is required"),
        ({"title": "x", "kind": "siren"}, "kind must be one of"),
        ({"title": "x", "severity": "loud"}, "severity must be one of"),
        ({"title": "x", "duration_seconds": "ten"}, "duration_seconds must be a number"),
        ({"title": "x", "cameras": ["camera.garage"]}, 'cameras only apply to kind "takeover"'),
        ({"title": "x", "wake": "yes"}, "wake must be true or false"),
        (["x"], "body must be a JSON object"),
    ],
)
def test_validation(body, error):
    notice, errors = parse_notice(body, CONFIG)
    assert notice is None and any(e.startswith(error) for e in errors), errors


def test_toast_duration_is_clamped_and_text_trimmed():
    notice, _ = parse_notice({"title": " Hi " + "x" * 300, "duration_seconds": 600}, CONFIG)
    assert notice["duration_seconds"] == 60
    assert len(notice["title"]) == 120 and notice["title"].startswith("Hi ")


async def test_a_non_json_body_is_a_400(app):
    r = await post(app, content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
