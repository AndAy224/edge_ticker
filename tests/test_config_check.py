"""backend/config_check.py: validate() and changed_paths()."""
from __future__ import annotations

import copy
from datetime import datetime as real_datetime

import pytest

from backend import db, scheduler as scheduler_module
from backend.config_check import changed_paths, validate
from backend.scheduler import NightScheduler
from helpers import FakeBus


def test_shipped_defaults_are_valid(defaults_config):
    assert validate(defaults_config) == []


def test_non_object_config():
    assert validate([]) == ["config must be a JSON object"]
    assert validate(None) == ["config must be a JSON object"]


@pytest.mark.parametrize(
    "order",
    [
        "markets,sports",
        ["markets", 3],
        None,
    ],
)
def test_rotation_order_must_be_list_of_strings(defaults_config, order):
    defaults_config["rotation"]["order"] = order
    assert validate(defaults_config) == ["rotation.order must be a list of module ids"]


@pytest.mark.parametrize("value", [4, 0, -1, "25", True, None])
def test_rotation_interval_floor(defaults_config, value):
    defaults_config["rotation"]["interval_seconds"] = value
    errors = validate(defaults_config)
    assert len(errors) == 1 and errors[0].startswith("rotation.interval_seconds")


@pytest.mark.parametrize("value", [5, 5.5, 3600])
def test_rotation_interval_ok(defaults_config, value):
    defaults_config["rotation"]["interval_seconds"] = value
    assert validate(defaults_config) == []


@pytest.mark.parametrize(
    "module,key,value",
    [
        ("news", "poll_seconds", 0),
        ("news", "poll_seconds", 4.9),
        ("sports", "poll_seconds_live", 2),
        ("sports", "poll_seconds_schedule", "1800"),
        ("weather", "poll_seconds", False),
    ],
)
def test_poll_seconds_floor(defaults_config, module, key, value):
    defaults_config["modules"][module][key] = value
    assert validate(defaults_config) == [f"modules.{module}.{key} must be a number ≥ 5"]


def test_poll_seconds_null_is_left_to_the_collector(defaults_config):
    # null passes the structural check; the collector constructor then decides.
    defaults_config["modules"]["news"]["poll_seconds"] = None
    errors = validate(defaults_config)
    assert errors and all(e.startswith("modules.news: TypeError") for e in errors)


def test_module_must_be_object(defaults_config):
    defaults_config["modules"]["news"] = ["not", "an", "object"]
    assert validate(defaults_config) == ["modules.news must be an object"]


@pytest.mark.parametrize("value", ["7:00", "24:00", "23:60", "2300", 2300, "11:00 PM"])
def test_night_times_must_be_zero_padded_24h(defaults_config, value):
    defaults_config["night"]["dim_at"] = value
    assert validate(defaults_config) == ["night.dim_at must be HH:MM (24-hour, zero-padded)"]


@pytest.mark.parametrize("value", ["", None, "00:00", "23:59"])
def test_night_times_ok(defaults_config, value):
    defaults_config["night"]["nightly_reload_at"] = value
    assert validate(defaults_config) == []


@pytest.mark.parametrize("value", [-1, 101, "10", True])
def test_night_levels(defaults_config, value):
    defaults_config["night"]["dim_level"] = value
    assert validate(defaults_config) == ["night.dim_level must be a number from 0 to 100"]


def test_night_method(defaults_config):
    defaults_config["night"]["method"] = "hdmi-cec"
    assert validate(defaults_config) == ["night.method must be ddc or software"]
    defaults_config["night"]["method"] = "software"
    assert validate(defaults_config) == []


@pytest.mark.parametrize("key", ["rotation", "modules", "night", "appearance", "ha"])
def test_sections_must_be_objects(defaults_config, key):
    defaults_config[key] = ["nope"]
    assert f"{key} must be an object" in validate(defaults_config)


def test_collector_constructor_errors_are_reported(defaults_config):
    defaults_config["modules"]["sports"]["days_back"] = "four"
    errors = validate(defaults_config)
    assert len(errors) == 1
    assert errors[0].startswith("modules.sports: ValueError:")
    assert "four" in errors[0]


def test_disabled_collectors_are_not_constructed(defaults_config):
    defaults_config["modules"]["sports"]["days_back"] = "four"
    defaults_config["modules"]["sports"]["enabled"] = False
    assert validate(defaults_config) == []


def test_location_errors_surface_through_dependent_collectors(defaults_config):
    # weather_alerts float()s the shared location; weather itself doesn't.
    defaults_config["modules"]["weather"]["latitude"] = "north-ish"
    errors = validate(defaults_config)
    assert any(e.startswith("modules.weather_alerts: ValueError") for e in errors)


def test_structural_errors_short_circuit_construction(defaults_config):
    defaults_config["modules"]["sports"]["days_back"] = "four"
    defaults_config["night"]["method"] = "nope"
    assert validate(defaults_config) == ["night.method must be ddc or software"]


def test_multiple_errors_are_all_reported(defaults_config):
    defaults_config["rotation"]["interval_seconds"] = 1
    defaults_config["night"]["dim_level"] = 500
    defaults_config["modules"]["news"]["poll_seconds"] = 0
    assert len(validate(defaults_config)) == 3


# ---- regressions (found as bugs by this suite, now fixed) ------------------


def test_null_modules_is_reported_not_raised():
    config = db.with_defaults({"modules": None})  # what PUT /api/config validates
    assert isinstance(validate(config), list)


def test_night_time_with_trailing_newline_is_rejected(defaults_config):
    defaults_config["night"]["nightly_reload_at"] = "04:00\n"
    assert validate(defaults_config) != []


class _NightClock(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return real_datetime(2026, 9, 25, 23, 30, tzinfo=tz)


async def test_config_that_validates_does_not_crash_the_scheduler(defaults_config, monkeypatch):
    defaults_config["night"]["dim_level"] = None
    monkeypatch.setattr(scheduler_module, "datetime", _NightClock)
    if validate(defaults_config):
        return  # rejecting it is a fine fix too
    night = NightScheduler(FakeBus(), lambda: defaults_config)

    async def no_ddc(level):
        return False

    monkeypatch.setattr(night, "_ddcutil", no_ddc)
    night.state()
    await night._tick("23:30")


# ---- changed_paths ------------------------------------------------------------


def test_changed_paths_identical():
    config = {"a": {"b": 1}, "c": [1, 2]}
    assert changed_paths(config, copy.deepcopy(config)) == []


def test_changed_paths_nested_and_sorted():
    old = {"modules": {"news": {"poll_seconds": 300}, "adsb": {"radius_km": 40}}, "night": {"dim_at": "23:00"}}
    new = copy.deepcopy(old)
    new["modules"]["news"]["poll_seconds"] = 600
    new["modules"]["adsb"]["radius_km"] = 20
    new["night"]["dim_at"] = "22:00"
    assert changed_paths(old, new) == [
        "modules.adsb.radius_km",
        "modules.news.poll_seconds",
        "night.dim_at",
    ]


def test_changed_paths_lists_compare_whole():
    old = {"rotation": {"order": ["markets", "sports"]}}
    new = {"rotation": {"order": ["sports", "markets"]}}
    assert changed_paths(old, new) == ["rotation.order"]


def test_changed_paths_added_removed_and_type_changes():
    old = {"ha": {"climate": "climate.hall"}, "appearance": {"theme": "midnight"}}
    new = {"ha": {"climate": "climate.hall", "media": "media_player.tv"}, "appearance": "frost"}
    assert changed_paths(old, new) == ["appearance", "ha.media"]


def test_changed_paths_root_and_limit():
    assert changed_paths(1, 2) == ["(root)"]
    old = {f"k{i:02}": i for i in range(20)}
    new = {f"k{i:02}": -i - 1 for i in range(20)}
    assert changed_paths(old, new) == [f"k{i:02}" for i in range(8)]
    assert len(changed_paths(old, new, limit=3)) == 3
