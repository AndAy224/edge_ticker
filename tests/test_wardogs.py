"""backend/collectors/wardogs.py against recorded wdstats.co.uk responses (the
user's own profile and the gold price, 2026-10-15). The clock and timezone are
pinned: XP gains are measured from local midnight and the last 7 days, and
level-ups expire after an hour."""
from __future__ import annotations

import copy
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.collectors import wardogs
from backend.collectors.wardogs import WardogsCollector, compact
from backend.config_check import validate
from helpers import json_response, load_json

NOW = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)  # 4 PM EDT
PLAYER_PATH = "/api/players/76561197988260454"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    state = {"now": NOW}
    monkeypatch.setattr(wardogs, "_now", lambda: state["now"])
    yield state
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def player(**changes) -> dict:
    data = copy.deepcopy(load_json("wdstats_player.json"))
    data.update(changes)
    return data


def gold(**changes) -> dict:
    data = copy.deepcopy(load_json("wdstats_gold.json"))
    data.update(changes)
    return data


def raw(p: dict | None = None, g: dict | None | str = "recorded") -> dict:
    return {"player": p if p is not None else player(), "gold": gold() if g == "recorded" else g}


def bumped(role: str, level: int, **changes) -> dict:
    data = player(**changes)
    for entry in data["roles"]:
        if entry["role"] == role:
            data["totalLevel"] += level - entry["level"]
            entry["level"] = level
    return data


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---- shape --------------------------------------------------------------

def test_shape_recorded_profile(defaults_config):
    payload = WardogsCollector(defaults_config).shape(raw())
    stage = payload.stage
    assert stage["player"] == {
        "id": "76561197988260454",
        "name": "AndAy",
        "avatar": "https://avatars.steamstatic.com/5a0c6c5ee6de5101889b18ea3099c1faf24b3f9a_full.jpg",
        "url": "https://wdstats.co.uk/player/76561197988260454",
    }
    assert (stage["level"], stage["xp"], stage["unlocks"], stage["cosmetics"]) == (120, 1697761, 75, 7)
    assert stage["ranks"] == {
        "level": {"rank": 255, "total": 893},
        "xp": {"rank": 260, "total": 893},  # wdstats calls the career-XP board "overall"
        "unlocks": {"rank": 417, "total": 893},
    }
    # The day Season 2 began, the profile still carries Season 1's final figures.
    assert (stage["season"], stage["current_season"], stage["season_final"]) == (1, 2, True)
    assert [c["label"] for c in stage["classes"]] == ["Pilot", "Medic", "Support", "Recon", "Assault", "Driver"]
    assault = next(c for c in stage["classes"] if c["key"] == "Infantry")
    assert (assault["icon"], assault["level"], assault["rank"], assault["total"]) == ("wd-assault", 9, 782, 893)
    assert stage["classes"][0] == {
        "key": "Pilot", "label": "Pilot", "icon": "wd-pilot", "level": 32, "xp": 612852,
        "rank": 67, "total": 808, "up": False,
    }
    assert stage["cash"] == {"amount": 1037538, "rank": 171, "total": 893}
    assert stage["gold_bars"] == {"count": 3, "rank": 140, "total": 893, "value": 3 * 253947}
    assert stage["gold"]["price"] == 253947
    assert (stage["gold"]["pct_1d"], stage["gold"]["pct_7d"], stage["gold"]["pct_30d"]) == (-9.91, -43.28, -87.98)
    assert stage["gold"]["change_1d"] == -27933
    assert (stage["gold"]["low30"], stage["gold"]["high30"]) == (166000, 2113114)
    assert len(stage["gold"]["spark"]) == 30 and stage["gold"]["spark"][-1] == 253947
    # One history point (first tracked at 02:11Z, before local midnight): no
    # baseline for the week, and nothing gained today.
    assert stage["gain"] == {"week": None, "today": 0}
    assert stage["synced_at"] == "2026-10-15T16:49:39Z"
    assert stage["tracked"] == 893
    assert (stage["sync_stale"], stage["sync_error"], stage["hidden"], stage["pending"]) == (False, None, False, False)
    assert stage["level_up"] is False


def test_tape_recorded_profile(defaults_config):
    tape = WardogsCollector(defaults_config).shape(raw()).tape
    assert [(t.text, t.accent, t.icon) for t in tape] == [
        ("AndAy · Wardog LVL 120 · 1.70M XP · #260/893 · S1 final", "neutral", "wardogs"),
        ("Gold bar $253,947 ▼ 9.9%", "down", "wd-gold"),
    ]


def test_rarest_unlocks_skip_cosmetics(defaults_config):
    data = player()
    cosmetic = next(i for i in data["items"] if i["category"] == "Cosmetics")
    cosmetic["owners"] = 1
    rarest = WardogsCollector(defaults_config).shape(raw(data)).stage["rarest"]
    assert len(rarest) == 5
    assert all(r["category"] != "Cosmetics" for r in rarest)
    assert [r["owners"] for r in rarest] == sorted(r["owners"] for r in rarest)
    assert rarest[0] == {"name": "Z20 Lakota [Miniguns]", "category": "Vehicles", "owners": 59}


@pytest.mark.parametrize("url", [
    "https://evil.example/avatar.jpg",
    "http://avatars.steamstatic.com/a.jpg",
    "javascript:alert(1)",
    None,
])
def test_avatar_must_be_https_steam(defaults_config, url):
    stage = WardogsCollector(defaults_config).shape(raw(player(avatarUrl=url))).stage
    assert stage["player"]["avatar"] is None


def test_current_season_is_not_final(defaults_config):
    payload = WardogsCollector(defaults_config).shape(raw(player(season=2)))
    assert payload.stage["season_final"] is False
    assert "final" not in payload.tape[0].text


def test_hidden_profile_has_no_stats_item(defaults_config):
    payload = WardogsCollector(defaults_config).shape(raw(player(hidden=True)))
    assert payload.stage["hidden"] is True
    assert [t.icon for t in payload.tape] == ["wd-gold"]


def test_no_gold_price_yet(defaults_config):
    payload = WardogsCollector(defaults_config).shape(raw(g=None))
    assert payload.stage["gold"] is None
    assert payload.stage["gold_bars"]["value"] is None
    assert [t.icon for t in payload.tape] == ["wardogs"]


def test_flat_gold_day_is_neutral(defaults_config):
    data = gold()
    data["changes"][0].update(change=0, percent=0.0)
    item = WardogsCollector(defaults_config).shape(raw(g=data)).tape[-1]
    assert (item.text, item.accent) == ("Gold bar $253,947", "neutral")


def test_xp_gains_from_history(defaults_config):
    history = [
        {"t": iso(NOW - timedelta(days=10)), "xp": 1000, "level": 10},
        {"t": iso(NOW - timedelta(days=3)), "xp": 5000, "level": 20},
        {"t": iso(NOW - timedelta(minutes=10)), "xp": 7500, "level": 25},
    ]
    payload = WardogsCollector(defaults_config).shape(raw(player(history=history)))
    assert payload.stage["gain"] == {"week": 6500, "today": 2500}
    assert "+6.5K XP this week" in payload.tape[0].text


def test_xp_gain_baseline_falls_back_to_first_point(defaults_config):
    history = [
        {"t": iso(NOW - timedelta(days=2)), "xp": 100, "level": 1},
        {"t": iso(NOW - timedelta(hours=1)), "xp": 400, "level": 2},
    ]
    gain = WardogsCollector(defaults_config).shape(raw(player(history=history))).stage["gain"]
    assert gain == {"week": 300, "today": 300}


# ---- level-ups ----------------------------------------------------------

def test_level_up_is_noted_for_an_hour(defaults_config, clock):
    collector = WardogsCollector(defaults_config)
    first = collector.shape(raw())
    assert not any(c["up"] for c in first.stage["classes"])
    assert all("→" not in t.text for t in first.tape)

    clock["now"] = NOW + timedelta(minutes=15)
    second = collector.shape(raw(bumped("Pilot", 33)))
    pilot = second.stage["classes"][0]
    assert (pilot["key"], pilot["level"], pilot["up"]) == ("Pilot", 33, True)
    assert second.stage["level_up"] is True
    assert second.stage["level"] == 121
    top = second.tape[0]
    assert (top.text, top.accent, top.icon, top.priority) == ("AndAy · Pilot → LVL 33", "up", "wd-pilot", 1)

    # A config-save restart keeps it (module-level state).
    clock["now"] = NOW + timedelta(minutes=30)
    assert WardogsCollector(defaults_config).shape(raw(bumped("Pilot", 33))).stage["classes"][0]["up"]

    clock["now"] = NOW + timedelta(minutes=76)
    later = collector.shape(raw(bumped("Pilot", 33)))
    assert not later.stage["classes"][0]["up"] and later.stage["level_up"] is False
    assert all("→" not in t.text for t in later.tape)


def test_repeat_level_up_replaces_the_earlier_one(defaults_config, clock):
    collector = WardogsCollector(defaults_config)
    collector.shape(raw())
    clock["now"] = NOW + timedelta(minutes=15)
    collector.shape(raw(bumped("Pilot", 33)))
    clock["now"] = NOW + timedelta(minutes=30)
    tape = collector.shape(raw(bumped("Pilot", 34))).tape
    assert [t.text for t in tape if "→" in t.text] == ["AndAy · Pilot → LVL 34"]


def test_new_season_resets_the_baseline(defaults_config, clock):
    collector = WardogsCollector(defaults_config)
    collector.shape(raw())
    # Season 2's first sync: every class restarts low — no false level-ups.
    season2 = bumped("Pilot", 2, season=2)
    for entry in season2["roles"]:
        entry["level"] = 1 if entry["role"] != "Pilot" else 2
    clock["now"] = NOW + timedelta(minutes=15)
    reset = collector.shape(raw(season2))
    assert not any(c["up"] for c in reset.stage["classes"])
    clock["now"] = NOW + timedelta(minutes=30)
    season2_up = copy.deepcopy(season2)
    next(e for e in season2_up["roles"] if e["role"] == "Medic")["level"] = 2
    assert [c["key"] for c in collector.shape(raw(season2_up)).stage["classes"] if c["up"]] == ["Medic"]


# ---- fetch --------------------------------------------------------------

def handler_for(player_response=None, gold_response=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == PLAYER_PATH:
            return player_response or json_response(player())
        if request.url.path == "/api/gold":
            return gold_response or json_response(gold())
        return httpx.Response(404)
    return handler


async def test_fetch_player_and_gold(http, defaults_config):
    http.handler = handler_for()
    collector = WardogsCollector(defaults_config)
    data = await collector.fetch()
    assert [(r.url.host, r.url.path) for r in http.requests] == [
        ("wdstats.co.uk", PLAYER_PATH), ("wdstats.co.uk", "/api/gold"),
    ]
    assert all(r.headers["user-agent"].startswith("edge-ticker") for r in http.requests)
    assert http.client_kwargs[0]["timeout"] == 15
    assert data["player"]["name"] == "AndAy" and data["gold"]["price"] == 253947
    assert collector.degraded is None


async def test_gold_failure_keeps_last_price(http, defaults_config):
    http.handler = handler_for()
    collector = WardogsCollector(defaults_config)
    await collector.fetch()
    http.handler = handler_for(gold_response=httpx.Response(500))
    data = await collector.fetch()
    assert data["gold"]["price"] == 253947
    assert collector.degraded == "gold price unavailable: HTTPStatusError"
    # Recovers on the next good poll.
    http.handler = handler_for()
    await collector.fetch()
    assert collector.degraded is None


async def test_gold_failure_with_nothing_cached(http, defaults_config):
    http.handler = handler_for(gold_response=json_response({"price": None}))
    collector = WardogsCollector(defaults_config)
    data = await collector.fetch()
    assert data["gold"] is None
    assert collector.degraded == "gold price unavailable: ValueError"
    assert collector.shape(data).stage["gold"] is None


async def test_untracked_player_is_a_clear_error(http, defaults_config):
    http.handler = handler_for(player_response=httpx.Response(404))
    with pytest.raises(RuntimeError, match="isn't tracked on wdstats.co.uk"):
        await WardogsCollector(defaults_config).fetch()


async def test_refusal_holds_off_for_a_day(http, defaults_config, clock):
    http.handler = handler_for(player_response=httpx.Response(403))
    collector = WardogsCollector(defaults_config)
    with pytest.raises(RuntimeError, match=r"refused this client \(403\).*until 2026-10-16 20:00 UTC"):
        await collector.fetch()
    assert len(http.requests) == 1
    # Retries inside the hold fail without a request — also from the fresh
    # collector a config save builds.
    http.handler = handler_for()
    clock["now"] = NOW + timedelta(hours=23)
    for retry in (collector, WardogsCollector(defaults_config)):
        with pytest.raises(RuntimeError, match="refused this client"):
            await retry.fetch()
    assert len(http.requests) == 1
    clock["now"] = NOW + timedelta(hours=24)
    data = await collector.fetch()
    assert data["player"]["name"] == "AndAy"
    assert len(http.requests) == 3


async def test_player_server_error_raises(http, defaults_config):
    http.handler = handler_for(player_response=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await WardogsCollector(defaults_config).fetch()


async def test_site_sync_error_is_degraded(http, defaults_config):
    http.handler = handler_for(
        player_response=json_response(player(stale=True, syncError="Steam profile is private"))
    )
    collector = WardogsCollector(defaults_config)
    data = await collector.fetch()
    assert collector.degraded == "wdstats sync: Steam profile is private"
    stage = collector.shape(data).stage
    assert (stage["sync_stale"], stage["sync_error"]) == (True, "Steam profile is private")


# ---- config -------------------------------------------------------------

@pytest.mark.parametrize("value", ["7656", "76561197988260454 ", "76561197988260454\n", "../gold", "ABCDEF123456"])
def test_bad_player_id_rejected(defaults_config, value):
    defaults_config["modules"]["wardogs"]["player_id"] = value
    if value.strip() == "76561197988260454":
        assert WardogsCollector(defaults_config).player_id == "76561197988260454"  # trimmed, accepted
        return
    with pytest.raises(ValueError):
        WardogsCollector(defaults_config)
    errors = validate(defaults_config)
    assert len(errors) == 1 and errors[0].startswith("modules.wardogs: ValueError:")


@pytest.mark.parametrize("value", ["", None])
def test_blank_player_id_uses_the_default(defaults_config, value):
    defaults_config["modules"]["wardogs"]["player_id"] = value
    assert WardogsCollector(defaults_config).player_id == "76561197988260454"


def test_wdstats_slug_accepted(defaults_config):
    defaults_config["modules"]["wardogs"]["player_id"] = "732ac4c2c3bb"
    assert WardogsCollector(defaults_config).player_id == "732ac4c2c3bb"


def test_poll_is_floored(defaults_config):
    assert WardogsCollector(defaults_config).interval == 900
    defaults_config["modules"]["wardogs"]["poll_seconds"] = 60
    assert WardogsCollector(defaults_config).interval == 300


def test_compact():
    assert [compact(n) for n in (1697761, 176953, 48213, 950, None)] == ["1.70M", "177K", "48.2K", "950", "—"]
