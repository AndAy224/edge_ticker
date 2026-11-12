"""backend/collectors/launches.py against one recorded Launch Library 2 response.
The clock and timezone are pinned so the live window and tape are deterministic."""
from __future__ import annotations

import copy
import os
import time
from datetime import datetime as real_datetime, timezone

import httpx
import pytest

from backend.collectors import launches as launches_module
from backend.collectors.launches import LaunchesCollector, _in_live_window
from helpers import json_response, load_json


@pytest.fixture(autouse=True)
def eastern():
    """Tape strings are local wall-clock times: pin the zone the appliance runs in."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def pin(monkeypatch, iso_utc: str) -> None:
    fixed = real_datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc)

    class Fixed(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(launches_module, "datetime", Fixed)


def results(*names: str) -> dict:
    raw = copy.deepcopy(load_json("ll2_upcoming.json"))
    if names:
        raw["results"] = [r for r in raw["results"] if any(n in r["name"] for n in names)]
    return raw


def test_shape_recorded_launches(monkeypatch, defaults_config):
    pin(monkeypatch, "2026-09-25T12:00:00")
    collector = LaunchesCollector(defaults_config)
    payload = collector.shape(results())
    launches = payload.stage["launches"]
    assert [l["provider"] for l in launches] == ["Rocket Lab", "SpaceX", "SpaceX", "SpaceX"]
    assert [l["florida"] for l in launches] == [False, False, True, True]
    assert [l["starship"] for l in launches] == [False, True, False, False]
    assert [len(l["boosters"]) for l in launches] == [0, 1, 1, 3]
    assert not any(l["live"] for l in launches) and payload.stage["live"] is False
    assert collector.interval == collector.idle_interval == 1800

    crew = launches[2]
    assert crew["mission"] == "Crew-13" and crew["orbit"] == "LEO"
    assert crew["location"] == "Cape Canaveral SFS, FL, USA"
    assert crew["programs"] == ["Commercial Crew Program", "International Space Station"]
    assert crew["boosters"][0]["serial"] == "B1101"
    assert crew["boosters"][0]["reused"] is True and crew["boosters"][0]["landing_type"] == "RTLS"
    assert crew["rocket"]["full_name"] == "Falcon 9 Block 5" and crew["rocket"]["total"] == 633

    heavy = launches[3]
    assert [b["landing_type"] for b in heavy["boosters"]] == ["RTLS", "RTLS", "EXP"]

    starship = launches[1]
    assert len(starship["mission_description"]) <= launches_module.DESCRIPTION_MAX
    assert starship["mission_description"].endswith("…")
    assert starship["status"] == "TBC" and starship["status_text"] == "To Be Confirmed"

    [item] = payload.tape
    assert item.text == "Rocket Lab: Electron | Owlright, Owlright, Owlright (StriX Launch 13) — Fri 8:26 PM"
    assert item.accent == "neutral"


def test_live_window_tightens_poll_and_flags_canaveral(monkeypatch, defaults_config):
    pin(monkeypatch, "2026-10-01T15:00:00")  # Crew-13 at T-10 min
    collector = LaunchesCollector(defaults_config)
    payload = collector.shape(results("Crew-13", "NROL-97"))
    assert [l["live"] for l in payload.stage["launches"]] == [True, False]
    assert payload.stage["live"] is True
    assert collector.interval == collector.live_interval == 360
    [item] = payload.tape
    assert item.text == "SpaceX: Falcon 9 Block 5 | Crew-13 — Thu 11:10 AM (Canaveral)"
    assert item.accent == "alert"


def test_idle_poll_wakes_when_the_live_window_opens(monkeypatch, defaults_config):
    # Crew-13 lifts off 15:10:06Z, so its live window opens 14:25:06Z. An idle
    # poll at 14:15 used to sleep until 14:45: the T-0 board started at T-25.
    pin(monkeypatch, "2026-10-01T14:15:00")
    collector = LaunchesCollector(defaults_config)
    payload = collector.shape(results("Crew-13", "NROL-97"))
    assert payload.stage["live"] is False
    assert collector.interval == 606 + launches_module.WAKE_MARGIN_SECONDS
    pin(monkeypatch, "2026-10-01T14:25:11")  # the early poll lands inside the window
    payload = collector.shape(results("Crew-13", "NROL-97"))
    assert payload.stage["live"] is True and collector.interval == collector.live_interval


def test_window_opening_beyond_the_idle_interval_changes_nothing(monkeypatch, defaults_config):
    pin(monkeypatch, "2026-10-01T13:50:00")  # opens in 35 min; the 14:20 idle poll comes first
    collector = LaunchesCollector(defaults_config)
    collector.shape(results("Crew-13", "NROL-97"))
    assert collector.interval == collector.idle_interval
    assert collector._woken_for == set()  # its early poll is still unspent


def test_early_poll_is_never_sooner_than_the_live_cadence(monkeypatch, defaults_config):
    pin(monkeypatch, "2026-10-01T14:24:00")  # the window opens in a minute
    collector = LaunchesCollector(defaults_config)
    collector.shape(results("Crew-13", "NROL-97"))
    assert collector.interval == collector.live_interval == 360


def test_slipping_launch_gets_one_early_poll_only(monkeypatch, defaults_config):
    """Launch Library allows ~15 requests an hour, shared with production: a
    NET that slips at every poll must not pull every poll forward."""
    pin(monkeypatch, "2026-10-01T14:15:00")
    collector = LaunchesCollector(defaults_config)
    raw = results("Crew-13", "NROL-97")
    collector.shape(raw)
    assert collector.interval < collector.idle_interval  # the one early poll
    for minutes in (20, 40, 60):  # each poll finds the NET moved out again
        slipped = copy.deepcopy(raw)
        slipped["results"][0]["net"] = f"2026-10-01T15:{10 + minutes // 2}:00Z"
        pin(monkeypatch, f"2026-10-01T14:{15 + minutes // 2}:00")
        payload = collector.shape(slipped)
        assert payload.stage["live"] is False
        assert collector.interval == collector.idle_interval
    # Once it has flown and left the list, its id is forgotten.
    collector.shape(results("NROL-97"))
    assert collector._woken_for == set()


def test_finished_launch_does_not_wake_the_poll(monkeypatch, defaults_config):
    pin(monkeypatch, "2026-10-01T14:15:00")
    raw = results("Crew-13", "NROL-97")
    raw["results"][0]["status"] = {"id": 3, "name": "Launch Successful", "abbrev": "Success"}
    collector = LaunchesCollector(defaults_config)
    collector.shape(raw)
    assert collector.interval == collector.idle_interval


def test_starship_flight_day_owns_the_tape(monkeypatch, defaults_config):
    pin(monkeypatch, "2026-09-28T10:00:00")  # 6 AM local; Starship NET 8:15 AM
    payload = LaunchesCollector(defaults_config).shape(results())
    [item] = payload.tape
    assert item.text.startswith("SpaceX: Starship | Starlink Group 31-1 (Starship Flight 14) — Mon 8:15 AM")
    assert item.accent == "alert"


def test_finished_launches_are_neither_live_nor_on_the_tape(monkeypatch, defaults_config):
    pin(monkeypatch, "2026-09-26T00:30:00")  # just after Electron's NET
    raw = results("Electron", "Crew-13")
    raw["results"][0]["status"] = {"id": 3, "name": "Launch Successful", "abbrev": "Success"}
    payload = LaunchesCollector(defaults_config).shape(raw)
    assert payload.stage["launches"][0]["live"] is False
    assert payload.tape[0].text.startswith("SpaceX: Falcon 9 Block 5 | Crew-13")


def test_rate_limit_guards(defaults_config):
    defaults_config["modules"]["launches"].update(poll_seconds=60, poll_seconds_live=30)
    collector = LaunchesCollector(defaults_config)
    assert collector.idle_interval == 240 and collector.live_interval == 240
    assert collector.backoff_start >= 900 and collector.backoff_max >= 3600


@pytest.mark.parametrize(
    "net,status,expected",
    [
        ("2026-10-01T15:44:00Z", "Go", True),  # T-44 min
        ("2026-10-01T15:46:00Z", "Go", False),  # T-46 min
        ("2026-10-01T14:46:00Z", "Go", True),  # T+14 min
        ("2026-10-01T14:44:00Z", "Go", False),  # T+16 min
        ("2026-10-01T15:10:00Z", "Success", False),
        (None, "Go", False),
        ("garbage", "Go", False),
    ],
)
def test_in_live_window(net, status, expected):
    now = real_datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
    assert _in_live_window(net, status, now) is expected


async def test_fetch_request_budget(http, monkeypatch, defaults_config):
    """Idle: one upcoming call per poll, recent results on the 1st and every 4th."""
    pin(monkeypatch, "2026-09-25T12:00:00")
    previous = {"results": [{"name": "Falcon 9 | Starlink", "lsp_name": "SpaceX",
                             "status": {"abbrev": "Success"}, "net": "2026-09-24T10:00:00Z"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/launch/upcoming/"):
            return json_response(results())
        if request.url.path.endswith("/launch/previous/"):
            return json_response(previous)
        return httpx.Response(404)

    http.handler = handler
    collector = LaunchesCollector(defaults_config)
    for _ in range(5):
        await collector.fetch()
    paths = [r.url.path.rsplit("/", 2)[-2] for r in http.requests]
    assert paths.count("upcoming") == 5 and paths.count("previous") == 2
    upcoming = next(r for r in http.requests if r.url.path.endswith("/upcoming/"))
    assert dict(upcoming.url.params) == {"limit": "12", "mode": "detailed", "hide_recent_previous": "true"}
    assert collector._recent == [
        {"name": "Falcon 9 | Starlink", "provider": "SpaceX", "status": "Success", "net": "2026-09-24T10:00:00Z"}
    ]
    assert collector.shape(results()).stage["recent"] == collector._recent


async def test_fetch_skips_recent_results_while_live(http, monkeypatch, defaults_config):
    pin(monkeypatch, "2026-10-01T15:00:00")
    http.handler = lambda request: json_response(
        results() if request.url.path.endswith("/upcoming/") else {"results": []}
    )
    collector = LaunchesCollector(defaults_config)
    collector._recent, collector._recent_tried = [{"name": "cached"}], True
    for _ in range(6):
        await collector.fetch()
    assert all(r.url.path.endswith("/upcoming/") for r in http.requests)


@pytest.mark.parametrize(
    "previous",
    [lambda: httpx.Response(500), lambda: json_response({"results": []})],
    ids=["failing", "empty"],
)
@pytest.mark.parametrize("now, upcoming_calls, previous_calls", [
    ("2026-09-25T12:00:00", 5, 2),  # idle: the 1st poll, then every 4th
    ("2026-10-01T15:00:00", 6, 1),  # live: only the startup attempt
])
async def test_failing_or_empty_recent_results_keep_their_cadence(
    http, monkeypatch, defaults_config, previous, now, upcoming_calls, previous_calls
):
    """Recent results that fail or come back empty used to be re-requested on
    every poll — two calls per poll, over Launch Library's ~15/hr when live."""
    pin(monkeypatch, now)
    http.handler = lambda request: (
        json_response(results()) if request.url.path.endswith("/upcoming/") else previous()
    )
    collector = LaunchesCollector(defaults_config)
    for _ in range(upcoming_calls):
        await collector.fetch()
    paths = [r.url.path.rsplit("/", 2)[-2] for r in http.requests]
    assert paths.count("upcoming") == upcoming_calls
    assert paths.count("previous") == previous_calls
    assert collector._recent == []
