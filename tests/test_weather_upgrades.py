"""The weather-module upgrades: NHC outlook areas and the home-in-cone threat
(hurricanes), nearby polygon warnings and expiry (weather_alerts), and the
beach & tides collector (marine)."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import httpx
import pytest

from backend.collectors import marine as marine_module
from backend.collectors import weather_alerts as alerts_module
from backend.collectors.hurricanes import (
    HurricanesCollector,
    parse_outlook_zip,
    point_in_polygon,
)
from backend.collectors.marine import MarineCollector, nearest_reference_station
from backend.collectors.weather_alerts import WeatherAlertsCollector
from helpers import load_bytes, load_json

# ---- NHC Graphical Tropical Weather Outlook ----------------------------------


def test_outlook_parses_an_active_atlantic_area():
    # 2025-09-25 23:35Z: the disturbance that became TD Nine, AL94, 80%/90%.
    outlook = parse_outlook_zip(load_bytes("nhc_gtwo_202509252335.zip"))
    assert outlook["issued"] == "2025-09-25T23:35:00+00:00"
    [area] = outlook["areas"]
    assert (area["area"], area["title"], area["invest"]) == ("1", "Southwestern Atlantic", "AL94")
    assert (area["prob2"], area["risk2"], area["prob7"], area["risk7"]) == (80, "High", 90, "High")
    assert 3 <= len(area["polygon"]) <= 80  # thinned for the display
    assert area["x"] == pytest.approx({"lat": 20.7335, "lon": -72.0519}, abs=1e-3)
    assert all(len(p) == 2 for p in area["polygon"])


def test_outlook_basin_filter_and_quiet_atlantic():
    bundle = load_bytes("nhc_gtwo_202609251140.zip")  # Atlantic quiet, two East Pacific areas
    assert parse_outlook_zip(bundle)["areas"] == []
    pacific = parse_outlook_zip(bundle, basin="Pacific")["areas"]
    assert [a["prob7"] for a in pacific] == [90, 50]  # most likely first
    assert pacific[0]["title"] is None  # titles come from the Atlantic text only
    assert pacific[0]["path"]  # motion arrow


def test_point_in_polygon():
    square = [[0, 0], [0, 10], [10, 10], [10, 0]]
    assert point_in_polygon(5, 5, square)
    assert not point_in_polygon(15, 5, square)
    assert not point_in_polygon(5, -1, square)


def storm(**geometry) -> dict:
    return {
        "id": "al092026", "name": "Ian", "classification": "HU", "intensity": 90,
        "latitudeNumeric": 24.0, "longitudeNumeric": -84.0, "movementDir": 0,
        "movementSpeed": 10, "pressure": 970,
        "_geometry": {"track": [], "cone": [], **geometry},
    }


def test_home_in_cone_raises_a_threat(defaults_config):
    collector = HurricanesCollector(defaults_config)  # Clearwater 27.97, -82.80
    cone = [[23, -86], [23, -82], [29, -81], [29, -84.5]]
    track = [{"lat": 26, "lon": -83.5}, {"lat": 28.2, "lon": -82.9}]
    payload = collector.shape([storm(cone=cone, track=track)])
    stage = payload.stage
    assert stage["threat"] == {"kind": "cone", "storm": "Ian", "class_text": "Hurricane"}
    assert stage["storms"][0]["home_in_cone"] is True
    assert stage["storms"][0]["closest_mi"] < 25  # the track passes right by
    assert payload.tape[0].text == "Clearwater is inside the forecast cone of Hurricane Ian"
    assert payload.tape[0].priority == 6


def test_cone_elsewhere_is_no_threat(defaults_config):
    far_cone = [[15, -60], [15, -55], [20, -55], [20, -60]]
    stage = HurricanesCollector(defaults_config).shape([storm(cone=far_cone)]).stage
    assert stage["threat"] is None
    assert stage["storms"][0]["home_in_cone"] is False


def test_outlook_areas_join_the_stage_and_tape(defaults_config):
    collector = HurricanesCollector(defaults_config)
    collector._outlook = parse_outlook_zip(load_bytes("nhc_gtwo_202509252335.zip"))
    payload = collector.shape([])
    stage = payload.stage
    assert stage["quiet"] is False  # no storms, but NHC is watching an area
    [area] = stage["outlook"]
    assert area["bearing_from_home"] == "SE" and 700 < area["distance_mi"] < 1000
    assert area["home_in_area"] is False
    assert payload.tape[0].text.startswith("NHC: 90% chance of development in 7 days — Southwestern Atlantic")
    assert payload.tape[0].accent == "alert"


async def test_outlook_refresh_is_conditional_and_keeps_the_last_good(http, defaults_config):
    bundle = load_bytes("nhc_gtwo_202509252335.zip")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("CurrentStorms.json"):
            return httpx.Response(200, json={"activeStorms": []})
        if len(calls) <= 2:
            return httpx.Response(200, content=bundle, headers={"last-modified": "Thu, 25 Sep 2025 23:40:00 GMT"})
        return httpx.Response(503)

    http.handler = handler
    collector = HurricanesCollector(defaults_config)
    await collector.fetch()
    assert [a["invest"] for a in collector._outlook["areas"]] == ["AL94"]
    assert collector.degraded is None

    collector._outlook_at = 0.0  # due again
    await collector.fetch()
    outlook_requests = [r for r in calls if r.url.path.endswith("gtwo_shapefiles.zip")]
    assert outlook_requests[-1].headers["if-modified-since"] == "Thu, 25 Sep 2025 23:40:00 GMT"
    assert [a["invest"] for a in collector._outlook["areas"]] == ["AL94"]  # 503: kept
    assert "outlook" in collector.degraded


# ---- NWS: expiry and nearby polygon warnings --------------------------------------

RECORDED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def alerts_clock(monkeypatch):
    monkeypatch.setattr(alerts_module, "_now", lambda: RECORDED_AT)


def texas_config(defaults_config, lat=29.3, lon=-103.4):
    # Home beside the recorded Flood Warning polygon in far west Texas.
    defaults_config["modules"]["weather"].update(latitude=lat, longitude=lon)
    return defaults_config


def nws_handler(state="TX", points_status=200):
    tx = load_json("nws_alerts_tx.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/points/" in request.url.path:
            if points_status != 200:
                return httpx.Response(points_status)
            return httpx.Response(200, json={"properties": {"relativeLocation": {"properties": {"state": state}}}})
        if request.url.params.get("area"):
            return httpx.Response(200, json=tx)
        return httpx.Response(200, json={"features": []})  # nothing at the point itself

    return handler


async def test_nearby_polygon_warnings(http, defaults_config, alerts_clock, fresh_overlay_state):
    http.handler = nws_handler()
    collector = WeatherAlertsCollector(texas_config(defaults_config))
    alerts = await collector.fetch()
    stage = collector.shape(alerts).stage
    assert stage["alerts"] == []
    [warning] = stage["nearby"]  # the watch and the AQ alert have no geometry
    assert warning["event"] == "Flood Warning"
    assert warning["km"] < 20
    assert len(warning["rings"]) == 1 and len(warning["rings"][0]) >= 3
    lat, lon = warning["rings"][0][0]
    assert 28 < lat < 30 and -104 < lon < -103  # [lat, lon] order for the display
    area_request = next(r for r in http.requests if r.url.params.get("area"))
    assert area_request.url.params["area"] == "TX"
    # the state lookup is cached for the collector's lifetime
    await collector.fetch()
    assert sum("/points/" in r.url.path for r in http.requests) == 1


async def test_nearby_respects_the_radius(http, defaults_config, alerts_clock, fresh_overlay_state):
    http.handler = nws_handler()
    config = texas_config(defaults_config, lat=31.8, lon=-106.4)  # El Paso, ~350 km away
    config["modules"]["weather_alerts"]["map_radius_km"] = 100
    collector = WeatherAlertsCollector(config)
    stage = collector.shape(await collector.fetch()).stage
    assert stage["nearby"] == []


async def test_nearby_failure_degrades_but_keeps_home_alerts(http, defaults_config, alerts_clock, fresh_overlay_state):
    http.handler = nws_handler(points_status=500)
    collector = WeatherAlertsCollector(texas_config(defaults_config))
    alerts = await collector.fetch()  # does not raise
    assert alerts == []
    assert collector.degraded.startswith("nearby warnings")


async def test_expired_alerts_drop_out(http, defaults_config, monkeypatch, fresh_overlay_state):
    http.handler = nws_handler()
    collector = WeatherAlertsCollector(texas_config(defaults_config))
    monkeypatch.setattr(alerts_module, "_now", lambda: RECORDED_AT)
    raw = await collector.fetch()
    assert collector.shape(raw).stage["nearby"]
    # 17:00Z: past the Flood Warning's 11:00 CDT end — gone without a refetch.
    monkeypatch.setattr(alerts_module, "_now", lambda: datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc))
    assert collector.shape(raw).stage["nearby"] == []
    feature = copy.deepcopy(load_json("nws_alerts_tx.json")["features"][1])
    assert WeatherAlertsCollector._parse(feature) is None


# ---- beach & tides -----------------------------------------------------------------

TIDES_AT = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc)  # 9 AM EDT, flood tide


@pytest.fixture
def tides_clock(monkeypatch):
    monkeypatch.setattr(marine_module, "_now", lambda: TIDES_AT)


def noaa_handler(water=True, marine_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path.endswith("stations.json"):
            return httpx.Response(200, json=load_json("noaa_tide_stations.json"))
        if path.endswith("datagetter"):
            if params["product"] == "water_temperature":
                return httpx.Response(200, json=load_json("noaa_water_temperature.json") if water else {"error": {"message": "No data was found."}})
            name = "noaa_predictions_hilo.json" if params["interval"] == "hilo" else "noaa_predictions_hourly.json"
            return httpx.Response(200, json=load_json(name))
        if "marine-api" in str(request.url):
            return httpx.Response(marine_status, json=load_json("open_meteo_marine.json"))
        return httpx.Response(200, json=load_json("open_meteo_uv.json"))

    return handler


def test_nearest_reference_station_skips_subordinates():
    stations = load_json("noaa_tide_stations.json")["stations"]
    best = nearest_reference_station(stations, 27.9659, -82.8001)
    # Subordinate "Clearwater" (S) is closer, but has no hourly curve.
    assert (best["id"], best["name"]) == ("8726724", "Clearwater Beach")
    assert best["distance_km"] == pytest.approx(3.4, abs=0.2)


def test_marine_ships_disabled(defaults_config):
    assert MarineCollector.enabled_by_default is False
    assert defaults_config["modules"]["marine"]["enabled"] is False


async def test_marine_fetch_and_shape(http, defaults_config, tides_clock):
    http.handler = noaa_handler()
    collector = MarineCollector(defaults_config)
    raw = await collector.fetch()
    assert collector.degraded is None
    predictions = [r for r in http.requests if r.url.params.get("product") == "predictions"]
    assert {r.url.params["time_zone"] for r in predictions} == {"gmt"}
    stage = collector.shape(raw).stage
    tide = stage["tide"]
    assert all(e["t"].endswith("Z") for e in tide["events"] + tide["curve"])  # UTC, unambiguous
    assert tide["next"]["type"] == "H" and tide["next"]["t"] == "2026-09-25T15:27:00Z"
    assert tide["rising"] is True
    assert 0.97 < tide["now_ft"] < 3.2  # between the 09:19Z low and the 15:27Z high
    assert tide["events"][0]["t"] == "2026-09-25T09:19:00Z"  # the one just passed leads
    assert stage["water_source"] == "station" and stage["water_f"] > 70
    assert stage["waves"]["from"] and stage["waves"]["ft"] is not None
    assert stage["uv"]["max"] is not None
    # The station list is fetched once per collector.
    await collector.fetch()
    assert sum(r.url.path.endswith("stations.json") for r in http.requests) == 1


async def test_marine_falls_back_and_degrades(http, defaults_config, tides_clock):
    http.handler = noaa_handler(water=False, marine_status=500)
    collector = MarineCollector(defaults_config)
    raw = await collector.fetch()
    assert collector.degraded == "waves: HTTPStatusError"
    stage = collector.shape(raw).stage
    assert stage["water_f"] is None and stage["waves"] is None  # no gauge, no model
    assert stage["tide"]["next"] is not None  # tides still publish


async def test_marine_station_override(http, defaults_config, tides_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stations/8726520.json"):
            return httpx.Response(200, json={"stations": [{"name": "St. Petersburg", "lat": 27.76, "lng": -82.63}]})
        return noaa_handler()(request)

    http.handler = handler
    defaults_config["modules"]["marine"]["station"] = "8726520"
    collector = MarineCollector(defaults_config)
    await collector.fetch()
    assert collector._station["name"] == "St. Petersburg"
    assert not any(r.url.path.endswith("stations.json") for r in http.requests)
