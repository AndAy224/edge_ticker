"""Weather suite collectors against recorded upstream responses: weather,
airquality, weather_alerts, weather_radar, hurricanes."""
from __future__ import annotations

import copy
import io
import zipfile

import pytest

from backend.collectors import weather_alerts as alerts_module
from backend.collectors.airquality import AirQualityCollector, aqi_band
from backend.collectors.hurricanes import (
    HurricanesCollector,
    compass,
    movement_phrase,
    parse_cone_kmz,
    parse_track_kmz,
    saffir_simpson,
)
from backend.collectors.weather import WeatherCollector
from backend.collectors.weather_alerts import WeatherAlertsCollector
from backend.collectors.weather_radar import MAX_PAST_FRAMES, WeatherRadarCollector
from helpers import FakeBus, json_response, load_json

# ---- weather (Open-Meteo forecast) --------------------------------------------------


def test_weather_shape(defaults_config):
    raw = load_json("open_meteo_forecast.json")
    payload = WeatherCollector(defaults_config).shape(raw)
    stage = payload.stage
    assert payload.module == "weather"
    assert stage["location"] == "Clearwater, FL"
    assert stage["current"] == {
        "temp": 69.2, "feels_like": 72.1, "humidity": 86, "wind": 5.9, "code": 0, "text": "Clear",
    }
    assert [d["date"] for d in stage["daily"]] == raw["daily"]["time"]
    assert stage["daily"][0] == {
        "date": "2026-09-25", "high": 79.9, "low": 66.9, "precip": 1, "code": 2, "text": "Partly cloudy",
    }
    # Next 24 hours starting at the current hour (current.time is 08:30).
    assert len(stage["hourly"]) == 24
    assert stage["hourly"][0]["time"] == "2026-09-25T08:00"
    assert stage["hourly"][-1]["time"] == "2026-09-26T07:00"
    assert stage["sun"] == {"sunrise": "2026-09-25T07:21", "sunset": "2026-09-25T19:24"}
    assert [t.text for t in payload.tape] == ["Clearwater, FL 69°F Clear"]


def test_weather_shape_tolerates_an_empty_response(defaults_config):
    payload = WeatherCollector(defaults_config).shape({})
    assert payload.stage["daily"] == [] and payload.stage["hourly"] == []
    assert payload.tape == []


async def test_weather_fetch_request(http, defaults_config):
    http.handler = lambda request: json_response(load_json("open_meteo_forecast.json"))
    raw = await WeatherCollector(defaults_config).fetch()
    assert raw["current"]["temperature_2m"] == 69.2
    params = http.requests[0].url.params
    assert (params["latitude"], params["longitude"]) == ("27.9659", "-82.8001")
    assert params["temperature_unit"] == "fahrenheit"
    assert http.client_kwargs[0]["timeout"] == 15


# ---- airquality (Open-Meteo AQ) ------------------------------------------------------


def test_airquality_shape(defaults_config):
    payload = AirQualityCollector(defaults_config).shape(load_json("open_meteo_air_quality.json"))
    stage = payload.stage
    assert stage["aqi"] == {"value": 30, "category": "Good", "accent": "up", "dominant": "O₃"}
    assert [p["key"] for p in stage["pollutants"]] == [
        "pm2_5", "pm10", "ozone", "nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide",
    ]
    assert stage["pollutants"][0] == {
        "key": "pm2_5", "label": "PM2.5", "value": 3.1, "unit": "μg/m³", "sub_aqi": 22,
    }
    assert stage["pollen"] == []  # Open-Meteo pollen is Europe-only
    assert len(stage["hourly"]) == 24 and stage["hourly"][0]["time"] == "2026-09-25T08:00"
    assert [(t.text, t.accent) for t in payload.tape] == [("Clearwater, FL AQI 30 — Good", "neutral")]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ("", "neutral")),
        (0, ("Good", "up")),
        (50, ("Good", "up")),
        (51, ("Moderate", "neutral")),
        (101, ("Unhealthy for sensitive groups", "alert")),
        (151, ("Unhealthy", "down")),
        (250, ("Very unhealthy", "down")),
        (400, ("Hazardous", "down")),
    ],
)
def test_aqi_bands(value, expected):
    assert aqi_band(value) == expected


def test_airquality_unhealthy_tape_is_alert(defaults_config):
    raw = load_json("open_meteo_air_quality.json")
    raw["current"]["us_aqi"] = 155
    payload = AirQualityCollector(defaults_config).shape(raw)
    assert payload.tape[0].accent == "alert"
    assert payload.stage["aqi"]["category"] == "Unhealthy"


def test_airquality_reuses_weather_location_unless_overridden(defaults_config):
    defaults_config["modules"]["weather"].update(latitude=40.7, longitude=-74.0, location_name="NYC")
    collector = AirQualityCollector(defaults_config)
    assert (collector.latitude, collector.longitude, collector.location_name) == (40.7, -74.0, "NYC")
    defaults_config["modules"]["airquality"]["location_name"] = "Office"
    assert AirQualityCollector(defaults_config).location_name == "Office"


# ---- weather_alerts (NWS) --------------------------------------------------------------


@pytest.fixture
def fresh_overlay_state(monkeypatch):
    monkeypatch.setattr(alerts_module, "_fired_ids", {})
    monkeypatch.setattr(alerts_module, "_event_fired_at", {})


def parsed_alerts() -> list[dict]:
    return [
        a
        for f in load_json("nws_alerts_tx.json")["features"]
        if (a := WeatherAlertsCollector._parse(f)) is not None
    ]


def test_alerts_parse_real_features():
    alerts = parsed_alerts()
    assert [a["event"] for a in alerts] == ["Flood Watch", "Flood Warning", "Air Quality Alert"]
    warning = alerts[1]
    assert warning["severity"] == "Severe"
    assert warning["id"].startswith("urn:oid:")
    assert warning["ends"] == "2026-09-25T11:00:00-05:00"
    assert warning["instruction"] and len(warning["instruction"]) <= 280  # truncated
    assert alerts[2]["ends"] == "2026-09-25T19:00:00-05:00"  # no `ends`: falls back to expires
    assert alerts[2]["instruction"] is None


@pytest.mark.parametrize(
    "field,value",
    [("status", "Test"), ("status", "Exercise"), ("messageType", "Cancel"), ("messageType", "Ack")],
)
def test_alerts_parse_drops_non_actual_messages(field, value):
    feature = copy.deepcopy(load_json("nws_alerts_tx.json")["features"][1])
    feature["properties"][field] = value
    assert WeatherAlertsCollector._parse(feature) is None


def test_alerts_shape(defaults_config):
    payload = WeatherAlertsCollector(defaults_config).shape(parsed_alerts())
    assert [t.text for t in payload.tape] == [
        "Flood Watch — until 12:00 AM",
        "Flood Warning — until 11:00 AM",
        "Air Quality Alert — until 7:00 PM",
    ]
    assert all((t.accent, t.priority, t.icon) == ("alert", 3, "warning") for t in payload.tape)
    assert payload.stage["location"] == "Clearwater, FL"
    assert len(payload.stage["alerts"]) == 3


def test_alerts_tape_is_capped(defaults_config):
    many = parsed_alerts() * 3
    payload = WeatherAlertsCollector(defaults_config).shape(many)
    assert len(payload.tape) == alerts_module.MAX_TAPE_ALERTS
    assert len(payload.stage["alerts"]) == 9


async def test_alerts_fetch_quiet_location(http, defaults_config, fresh_overlay_state):
    http.handler = lambda request: json_response(load_json("nws_alerts_point.json"))
    collector = WeatherAlertsCollector(defaults_config)
    collector._bus = FakeBus()
    alerts = await collector.fetch()
    assert alerts == []
    request = http.requests[0]
    assert request.url.params["point"] == "27.9659,-82.8001"
    assert request.headers["user-agent"].startswith("edge-ticker/")
    payload = collector.shape(alerts)
    assert payload.tape == [] and payload.stage["alerts"] == []


def test_alerts_point_rounded_to_four_decimals(defaults_config):
    defaults_config["modules"]["weather"].update(latitude=27.965912345, longitude=-82.800123456)
    collector = WeatherAlertsCollector(defaults_config)
    assert (collector.latitude, collector.longitude) == (27.9659, -82.8001)


async def test_overlay_fires_for_new_severe_warnings_only(defaults_config, fresh_overlay_state):
    bus = FakeBus()
    collector = WeatherAlertsCollector(defaults_config)
    collector._bus = bus
    await collector._maybe_overlay(parsed_alerts())
    fired = bus.of_type("weather_alert")
    assert [m["alert"]["event"] for m in fired] == ["Flood Warning"]  # not the watch
    # Same alert on the next poll: no replay.
    await collector._maybe_overlay(parsed_alerts())
    assert len(bus.of_type("weather_alert")) == 1
    # NWS re-issues an update under a new id: cooldown per event name holds.
    update = copy.deepcopy(parsed_alerts()[1])
    update["id"] = "urn:oid:new-update"
    await collector._maybe_overlay([update])
    assert len(bus.of_type("weather_alert")) == 1
    # A restarted collector (config save) doesn't replay it either.
    restarted = WeatherAlertsCollector(defaults_config)
    restarted._bus = bus
    await restarted._maybe_overlay(parsed_alerts())
    assert len(bus.of_type("weather_alert")) == 1


async def test_overlay_can_be_disabled(defaults_config, fresh_overlay_state):
    defaults_config["modules"]["weather_alerts"]["overlay"] = False
    bus = FakeBus()
    collector = WeatherAlertsCollector(defaults_config)
    collector._bus = bus
    await collector._maybe_overlay(parsed_alerts())
    assert bus.messages == []


# ---- weather_radar (RainViewer) ------------------------------------------------------------


def test_radar_shape(defaults_config):
    raw = load_json("rainviewer_weather_maps.json")
    payload = WeatherRadarCollector(defaults_config).shape(raw)
    stage = payload.stage
    past = raw["radar"]["past"]
    assert len(past) > MAX_PAST_FRAMES  # the recording has more frames than we keep
    assert [f["time"] for f in stage["frames"]] == [f["time"] for f in past[-MAX_PAST_FRAMES:]]
    assert not any(f["nowcast"] for f in stage["frames"])
    assert stage["host"] == "https://tilecache.rainviewer.com"
    assert stage["center"] == {"lat": 27.9659, "lon": -82.8001}
    assert stage["zoom"] == 7.5 and stage["color"] == 4
    assert payload.tape == []


def test_radar_nowcast_and_malformed_frames(defaults_config):
    raw = load_json("rainviewer_weather_maps.json")
    raw["radar"]["nowcast"] = [
        {"time": 1790340000, "path": "/v2/radar/nowcast1"},
        {"time": None, "path": "/v2/radar/broken"},
        {"time": 1790340600},
    ]
    del raw["host"]
    stage = WeatherRadarCollector(defaults_config).shape(raw).stage
    assert stage["frames"][-1] == {"time": 1790340000, "path": "/v2/radar/nowcast1", "nowcast": True}
    assert len(stage["frames"]) == MAX_PAST_FRAMES + 1
    assert stage["host"] == "https://tilecache.rainviewer.com"


# ---- hurricanes (NHC) -----------------------------------------------------------------------


def kmz(kml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("doc.kml", kml)
    return buffer.getvalue()


TRACK_KML = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
 <Placemark><name>Line</name><LineString><coordinates>-42.8,29.8 -44,30</coordinates></LineString></Placemark>
 <Placemark><name>11 AM Fri</name><Point><coordinates>-42.8,29.8,0</coordinates></Point></Placemark>
 <Placemark><name>8 AM Sat</name><Point><coordinates>-44.1,30.6,0</coordinates></Point></Placemark>
</Document></kml>"""

CONE_KML = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><Polygon><outerBoundaryIs><LinearRing>
 <coordinates>-42,29 -45,31 -46,29 -42,29</coordinates>
</LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"""


def test_kmz_parsers():
    assert parse_track_kmz(kmz(TRACK_KML)) == [
        {"lat": 29.8, "lon": -42.8, "label": "11 AM Fri"},
        {"lat": 30.6, "lon": -44.1, "label": "8 AM Sat"},
    ]
    assert parse_cone_kmz(kmz(CONE_KML)) == [[29.0, -42.0], [31.0, -45.0], [29.0, -46.0], [29.0, -42.0]]


def test_hurricanes_shape(defaults_config):
    raw = [s for s in load_json("nhc_current_storms.json")["activeStorms"] if s["id"].startswith("al")]
    payload = HurricanesCollector(defaults_config).shape(raw)
    stage = payload.stage
    assert stage["quiet"] is False
    assert [s["name"] for s in stage["storms"]] == ["Fay", "Gonzalo"]  # nearest first
    fay = stage["storms"][0]
    assert fay["class"] == "TS" and fay["class_text"] == "Tropical Storm"
    assert fay["category"] == "TS" and fay["wind_mph"] == 52
    assert fay["movement_dir"] == "W" and fay["movement_mph"] == 6
    assert fay["bearing_from_home"] == "ENE"  # great-circle initial bearing
    assert 2000 < fay["distance_mi"] < 2500
    assert fay["track"] == [] and fay["cone"] == []  # no KMZ geometry in this input
    assert stage["storms"][0]["distance_mi"] < stage["storms"][1]["distance_mi"]
    text = payload.tape[0].text
    assert text.startswith("TS Fay — 52 mph, ") and text.endswith("mi ENE, moving W 6 mph")
    assert all((t.accent, t.priority) == ("alert", 5) for t in payload.tape)


def test_hurricanes_quiet_basin(defaults_config):
    payload = HurricanesCollector(defaults_config).shape([])
    assert payload.stage["quiet"] is True and payload.stage["storms"] == []
    assert payload.stage["home"] == {"lat": 27.9659, "lon": -82.8001}
    assert payload.tape == []


def test_hurricanes_skip_storms_without_position(defaults_config):
    raw = copy.deepcopy(load_json("nhc_current_storms.json")["activeStorms"][:1])
    raw[0]["latitudeNumeric"] = None
    assert HurricanesCollector(defaults_config).shape(raw).stage["quiet"] is True


@pytest.mark.parametrize(
    "knots,label",
    [(33, "TD"), (34, "TS"), (63, "TS"), (64, "CAT 1"), (83, "CAT 2"), (96, "CAT 3"), (113, "CAT 4"), (137, "CAT 5")],
)
def test_saffir_simpson(knots, label):
    assert saffir_simpson(knots) == label


def test_motion_helpers():
    assert movement_phrase("NW", None) == "movement TBD"
    assert movement_phrase("NW", 0) == "stationary"
    assert movement_phrase("NW", 12) == "moving NW 12 mph"
    assert compass(None) == "" and compass(0) == "N" and compass(270) == "W" and compass(359) == "N"
