"""backend/collectors/adsb.py against a recorded adsb.lol response near Clearwater."""
from __future__ import annotations

import pytest

from backend.collectors.adsb import AdsbCollector, haversine_km
from helpers import json_response, load_json

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
        "https://api.adsb.lol/v2/lat/27.9659/lon/-82.8001/dist/22"
    )
    module["provider"] = "adsbfi"
    assert AdsbCollector(defaults_config)._url().startswith("https://opendata.adsb.fi/api/v3/")
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
