"""scripts/smoke_test.py and scripts/unit_checks.py write to the backend they
are pointed at (a config PUT, a `next` control, a made-up display_state): like
the layout audit they must refuse one that is not in fixture mode."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from helpers import json_response

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class Reached(Exception):
    """The script got as far as opening its WebSocket."""


def load(name: str, monkeypatch):
    # unit_checks edits sys.path and setdefaults ADSB_URL on import.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("ADSB_URL", "http://example.invalid/aircraft.json")
    spec = importlib.util.spec_from_file_location(f"dev_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def connect(*args, **kwargs):
        raise Reached

    monkeypatch.setattr(module.websockets, "connect", connect)
    return module


def backend(http, *, fixture: bool) -> None:
    replies = {
        "/api/health": {"fixture": fixture, "collectors": []},
        "/api/config": {"rotation": {"order": []}},
        "/api/control": {"ok": True},
        "/api/ha/entities": {"status": "disabled"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and b"nope" in request.content:
            return json_response({"detail": "unknown action"}, status=400)
        return json_response(replies.get(request.url.path, {}))

    http.handler = handler


def writes(http) -> list[str]:
    return [f"{r.method} {r.url.path}" for r in http.requests if r.method != "GET"]


@pytest.mark.parametrize("script", ["smoke_test", "unit_checks"])
async def test_a_live_backend_is_refused_before_anything_is_sent(script, http, monkeypatch):
    monkeypatch.delenv("TICKER_ALLOW_LIVE", raising=False)
    backend(http, fixture=False)
    module = load(script, monkeypatch)
    with pytest.raises(SystemExit) as refusal:
        await module.main()
    assert "not a fixture-mode backend" in str(refusal.value)
    assert [f"{r.method} {r.url.path}" for r in http.requests] == ["GET /api/health"]


@pytest.mark.parametrize("script", ["smoke_test", "unit_checks"])
async def test_a_fixture_backend_is_exercised(script, http, monkeypatch):
    monkeypatch.delenv("TICKER_ALLOW_LIVE", raising=False)
    backend(http, fixture=True)
    module = load(script, monkeypatch)
    with pytest.raises(Reached):
        await module.main()
    if script == "smoke_test":
        assert writes(http)[0] == "POST /api/control"


async def test_the_override_runs_against_a_live_backend(http, monkeypatch):
    monkeypatch.setenv("TICKER_ALLOW_LIVE", "1")
    backend(http, fixture=False)
    module = load("smoke_test", monkeypatch)
    with pytest.raises(Reached):
        await module.main()
    assert writes(http)[0] == "POST /api/control"
