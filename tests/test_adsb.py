"""backend/collectors/adsb.py against a recorded adsb.lol response near Clearwater."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.collectors import adsb
from backend.collectors.adsb import AdsbCollector, haversine_km
from helpers import json_response, load_json

# adsb.lol's envoy, out of its 500/day budget at 11:08 UTC (recorded 2026-10-19).
LOL_429_HEADERS = {
    "x-envoy-ratelimited": "true",
    "x-ratelimit-limit": "500, 4;w=1, 60;w=60, 500;w=86400",
    "x-ratelimit-remaining": "0",
    "x-ratelimit-reset": "46330",
}
NOW = datetime(2026, 10, 19, 11, 8, 30, tzinfo=timezone.utc)

HOME = (27.9659, -82.8001)


@pytest.fixture
def collector(defaults_config) -> AdsbCollector:
    defaults_config["modules"]["adsb"]["enabled"] = True
    return AdsbCollector(defaults_config)


def test_shape_recorded_traffic(collector):
    raw = load_json("adsblol_clearwater.json")
    in_radius = [
        p for p in raw["ac"]
        if p.get("lat") is not None and haversine_km(*HOME, p["lat"], p["lon"]) <= collector.radius_km
    ]
    grounded = [p for p in in_radius if p.get("alt_baro") == "ground"]
    assert grounded, "recording should include parked/taxiing aircraft near TPA"

    payload = collector.shape(raw)
    stage = payload.stage
    aircraft = stage["aircraft"]
    assert stage["count_in_radius"] == len(in_radius) == len(aircraft)
    assert stage["count_airborne"] == len(in_radius) - len(grounded)
    assert stage["count_total"] == raw["total"]
    assert stage["center"] == {"lat": HOME[0], "lon": HOME[1]}

    # Airborne first (nearest first), then everything on the ground.
    flags = [a["on_ground"] for a in aircraft]
    assert flags == sorted(flags)
    airborne = [a for a in aircraft if not a["on_ground"]]
    assert [a["distance_km"] for a in airborne] == sorted(a["distance_km"] for a in airborne)
    for plane in aircraft:
        if plane["on_ground"]:
            assert plane["alt_ft"] == 0
        assert plane["flight"] == plane["flight"].strip() and plane["flight"]
        assert plane["direction"] in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}

    # Tape: the three nearest *airborne* contacts, never a parked jet.
    assert len(payload.tape) == 3
    ground_callsigns = {a["flight"] for a in aircraft if a["on_ground"]}
    for item, plane in zip(payload.tape, airborne[:3]):
        assert item.text.startswith(f"{plane['flight']} · ")
        assert item.text.split(" · ")[0] not in ground_callsigns


def test_grounded_nearest_contacts_do_not_crowd_out_the_tape(collector):
    raw = {
        "ac": [
            {"hex": "g1", "flight": "PARK1", "lat": HOME[0], "lon": HOME[1] + 0.001, "alt_baro": "ground"},
            {"hex": "g2", "flight": "PARK2", "lat": HOME[0], "lon": HOME[1] + 0.002, "alt_baro": "ground"},
            {"hex": "a1", "flight": "FLY1", "lat": HOME[0] + 0.2, "lon": HOME[1], "alt_baro": 9000},
        ]
    }
    payload = collector.shape(raw)
    assert [a["flight"] for a in payload.stage["aircraft"]] == ["FLY1", "PARK1", "PARK2"]
    assert [t.text.split(" · ")[0] for t in payload.tape] == ["FLY1"]
    assert payload.stage["count_airborne"] == 1 and payload.stage["count_total"] == 3


def test_local_receiver_shape_and_fallbacks(collector):
    raw = {
        "aircraft": [
            {"hex": "abc123", "lat": 28.0, "lon": -82.8, "alt_geom": 5000, "geom_rate": -640},
            {"hex": "far", "lat": 35.0, "lon": -80.0, "alt_baro": 30000},
            {"hex": "nopos"},
        ]
    }
    stage = collector.shape(raw).stage
    [plane] = stage["aircraft"]
    assert plane["flight"] == "abc123"  # no callsign or registration: hex
    assert plane["alt_ft"] == 5000 and plane["vert_rate"] == -640
    assert stage["count_total"] == 3


def test_provider_urls(defaults_config, monkeypatch):
    module = defaults_config["modules"]["adsb"]
    assert AdsbCollector(defaults_config)._url() == (
        "https://opendata.adsb.fi/api/v3/lat/27.9659/lon/-82.8001/dist/22"
    )
    module["provider"] = "adsblol"
    assert AdsbCollector(defaults_config)._url().startswith("https://api.adsb.lol/v2/")
    module["provider"] = "nope"
    with pytest.raises(RuntimeError, match="unknown adsb provider"):
        AdsbCollector(defaults_config)._url()
    module["provider"] = "local"
    with pytest.raises(RuntimeError, match="ADSB_URL"):
        AdsbCollector(defaults_config)._url()
    monkeypatch.setenv("ADSB_URL", "http://pi-adsb.invalid/data/aircraft.json")
    assert AdsbCollector(defaults_config)._url() == "http://pi-adsb.invalid/data/aircraft.json"


async def test_fetch(http, collector):
    http.handler = lambda request: json_response(load_json("adsblol_clearwater.json"))
    raw = await collector.fetch()
    assert raw["total"] == 17
    assert http.requests[0].headers["user-agent"].startswith("edge-ticker/")
    assert http.client_kwargs[0]["timeout"] == 10


@pytest.fixture
def clock(monkeypatch):
    now = {"t": NOW}
    monkeypatch.setattr(adsb, "_now", lambda: now["t"])
    return now


def _by_host(responses: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        result = responses[request.url.host]
        if isinstance(result, Exception):
            raise result
        return result
    return handler


async def test_rate_limited_provider_falls_back_until_reset(http, defaults_config, clock):
    defaults_config["modules"]["adsb"]["provider"] = "adsblol"
    collector = AdsbCollector(defaults_config)
    traffic = load_json("adsblol_clearwater.json")
    http.handler = _by_host({
        "api.adsb.lol": json_response({"error": "rate limited"}, 429, LOL_429_HEADERS),
        "opendata.adsb.fi": json_response(traffic),
    })

    assert (await collector.fetch())["total"] == 17
    assert [r.url.host for r in http.requests] == ["api.adsb.lol", "opendata.adsb.fi"]
    # x-ratelimit-reset is seconds from now: the daily window ends at 00:00 UTC.
    assert collector.degraded == "adsb.lol rate-limited until 00:00 UTC — using adsb.fi"

    # Inside the window adsb.lol isn't asked again, even by a restarted collector.
    collector = AdsbCollector(defaults_config)
    clock["t"] = NOW + timedelta(hours=12)
    await collector.fetch()
    assert [r.url.host for r in http.requests[2:]] == ["opendata.adsb.fi"]
    assert collector.degraded.startswith("adsb.lol rate-limited until 00:00 UTC")

    # After the reset it is the primary again, and the module is healthy.
    http.handler = lambda request: json_response(traffic)
    clock["t"] = NOW + timedelta(hours=13)
    await collector.fetch()
    assert http.requests[-1].url.host == "api.adsb.lol"
    assert collector.degraded is None


async def test_transient_failure_skips_provider_briefly(http, collector, clock):
    traffic = load_json("adsblol_clearwater.json")
    http.handler = _by_host({
        "opendata.adsb.fi": httpx.ReadTimeout("slow"),
        "api.adsb.lol": json_response(traffic),
    })
    await collector.fetch()
    assert collector.degraded == "adsb.fi failed: ReadTimeout — using adsb.lol"

    clock["t"] = NOW + timedelta(minutes=1)
    await collector.fetch()
    assert [r.url.host for r in http.requests] == ["opendata.adsb.fi", "api.adsb.lol", "api.adsb.lol"]

    http.handler = lambda request: json_response(traffic)
    clock["t"] = NOW + timedelta(minutes=3)
    await collector.fetch()
    assert http.requests[-1].url.host == "opendata.adsb.fi" and collector.degraded is None


async def test_every_provider_down_raises_with_each_reason(http, collector, clock):
    http.handler = _by_host({
        "opendata.adsb.fi": json_response({}, 503),
        "api.adsb.lol": json_response({}, 429, {"retry-after": "120"}),
    })
    with pytest.raises(RuntimeError, match=r"adsb.fi failed: HTTPStatusError; "
                                           r"adsb.lol rate-limited until 11:10 UTC"):
        await collector.fetch()
    assert collector.degraded is None

    # Both still skipped: no request at all, same story.
    with pytest.raises(RuntimeError, match="adsb.lol rate-limited"):
        await collector.fetch()
    assert len(http.requests) == 2


def test_rate_limit_reset_headers():
    def reset(headers):
        return adsb._rate_limit_reset(httpx.Response(429, headers=headers), NOW) - NOW

    assert reset({"retry-after": "90"}) == timedelta(seconds=90)
    assert reset({"retry-after": "Mon, 19 Oct 2026 11:38:30 GMT"}) == timedelta(minutes=30)
    assert reset({"x-ratelimit-reset": str(NOW.timestamp() + 600)}) == timedelta(minutes=10)
    assert reset({}) == adsb.RATE_LIMIT_DEFAULT
    assert reset({"retry-after": "1"}) == timedelta(minutes=1)
    assert reset({"x-ratelimit-reset": "999999"}) == adsb.RATE_LIMIT_MAX


async def test_local_receiver_has_no_fallback(http, defaults_config, monkeypatch):
    monkeypatch.setenv("ADSB_URL", "http://pi-adsb.invalid/data/aircraft.json")
    defaults_config["modules"]["adsb"]["provider"] = "local"
    collector = AdsbCollector(defaults_config)
    http.handler = lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))
    for _ in range(2):  # no skip window either: every poll retries the receiver
        with pytest.raises(httpx.ConnectError):
            await collector.fetch()
    assert [r.url.host for r in http.requests] == ["pi-adsb.invalid"] * 2
