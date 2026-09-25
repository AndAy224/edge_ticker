"""Collectors whose upstreams must never be contacted from dev (Finnhub, Proxmox,
UniFi, ESPN fantasy) — shaped from small hand-written inputs instead. Plus the
astro moon-phase math."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from backend.collectors.astro import moon_phase
from backend.collectors.fantasy import FantasyCollector, win_probability
from backend.collectors.proxmox import ProxmoxCollector
from backend.collectors.stocks import SPARK_POINTS, MarketsCollector
from helpers import json_response

# ---- markets ---------------------------------------------------------------------------


def quote(symbol: str, price: float, change: float, pct: float) -> dict:
    return {"symbol": symbol, "price": price, "change": change, "pct": pct, "spark": []}


def test_markets_shape(defaults_config):
    defaults_config["modules"]["markets"]["featured"] = ["spy", "BTC-USD"]
    collector = MarketsCollector(defaults_config)
    collector._earnings_today = {"NVDA"}
    quotes = [quote("SPY", 5012.345, 12.5, 0.25), quote("TSLA", 180.0, -3.6, -1.96), quote("FLAT", 10, 0, 0)]
    payload = collector.shape(quotes)
    assert payload.stage == {"quotes": quotes, "featured": ["SPY", "BTC-USD"]}
    assert [(t.text, t.accent) for t in payload.tape] == [
        ("NVDA reports earnings today", "alert"),
        ("SPY 5,012.35 ▲ 0.25%", "up"),
        ("TSLA 180.00 ▼ 1.96%", "down"),
        ("FLAT 10.00 ▲ 0.00%", "up"),
    ]
    assert payload.tape[0].priority == 1


def test_markets_stream_symbol_mapping():
    assert MarketsCollector._stream_symbol("BTC-USD") == "BINANCE:BTCUSDT"
    assert MarketsCollector._stream_symbol("AAPL") == "AAPL"


def test_markets_spark_downsamples_history(defaults_config, tmp_path):
    collector = MarketsCollector(defaults_config)
    collector._spark_file = tmp_path / "spark.json"
    now = 1_800_000_000.0
    for i in range(200):
        collector._record("SPY", float(i), now + i)
    spark = collector._spark("SPY")
    assert len(spark) == SPARK_POINTS
    assert spark[0] == 0.0 and spark[-1] == 199.0
    collector._save_history()
    assert collector._spark_file.exists()


async def test_finnhub_quote_ipo_day_measures_from_open(defaults_config, monkeypatch):
    monkeypatch.setenv("FINNHUB_KEY", "test-key")  # only ever sent to the mock below
    collector = MarketsCollector(defaults_config)
    body = {"c": 33.0, "d": None, "dp": None, "o": 30.0, "h": 34.0, "l": 29.0, "pc": 0}
    seen = []

    def handler(request):
        seen.append(request)
        return json_response(body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        q = await collector._finnhub_quote(client, "NEWCO")
    assert q["prev_close"] == 30.0 and q["change"] == pytest.approx(3.0)
    assert q["pct"] == pytest.approx(10.0)
    assert seen[0].url.params["symbol"] == "NEWCO"

    body.update(c=0)  # Finnhub answers zeros for unknown symbols
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="no Finnhub quote"):
            await collector._finnhub_quote(client, "NOPE")


# ---- proxmox -------------------------------------------------------------------------------

GIB = 2**30


@pytest.fixture
def pve_env(monkeypatch):
    monkeypatch.setenv("PVE_URL", "https://pve.invalid:8006/")
    monkeypatch.setenv("PVE_TOKEN_ID", "ticker@pve!ro")
    monkeypatch.setenv("PVE_TOKEN_SECRET", "not-a-secret")


RESOURCES = [
    {"type": "node", "node": "pve1", "status": "online", "cpu": 0.42, "mem": 8 * GIB,
     "maxmem": 16 * GIB, "uptime": 200000},
    {"type": "qemu", "vmid": 100, "name": "homeassistant", "status": "running", "cpu": 0.05,
     "mem": 2 * GIB, "maxmem": 4 * GIB},
    {"type": "lxc", "vmid": 101, "status": "running", "cpu": 0.30, "mem": GIB, "maxmem": 2 * GIB},
    {"type": "qemu", "vmid": 102, "name": "win11", "status": "stopped"},
    {"type": "storage", "storage": "local-zfs", "node": "pve1", "disk": 100 * GIB, "maxdisk": 200 * GIB},
    {"type": "storage", "storage": "backup", "node": "pve1", "disk": 90 * GIB, "maxdisk": 100 * GIB},
    {"type": "storage", "storage": "offline", "node": "pve1", "disk": 0, "maxdisk": 0},
]


def test_proxmox_shape(pve_env, defaults_config):
    collector = ProxmoxCollector(defaults_config)
    assert collector.base_url == "https://pve.invalid:8006"
    assert collector.pdu_enabled is False  # no UniFi env
    payload = collector.shape({"resources": RESOURCES, "pdu": None})
    stage = payload.stage
    assert stage["nodes"] == [{
        "name": "pve1", "online": True, "cpu": 42.0, "mem_pct": 50.0,
        "mem_used_gb": 8.0, "mem_total_gb": 16.0, "uptime": 200000,
    }]
    assert stage["guests"]["running"] == 2 and stage["guests"]["total"] == 3
    assert [g["name"] for g in stage["guests"]["busiest"]] == ["lxc-101", "homeassistant"]
    assert [s["name"] for s in stage["storage"]] == ["backup", "local-zfs"]  # fullest first
    assert stage["storage"][1]["pct"] == 50.0 and stage["storage"][1]["free_gb"] == 100.0
    assert stage["power"] is None
    assert [(t.text, t.accent) for t in payload.tape] == [("PVE pve1: CPU 42% · MEM 50%", "neutral")]


def test_proxmox_offline_node_is_an_alert(pve_env, defaults_config):
    node = dict(RESOURCES[0], status="offline")
    payload = ProxmoxCollector(defaults_config).shape({"resources": [node], "pdu": None})
    assert payload.tape[0].text == "PVE pve1: OFFLINE" and payload.tape[0].accent == "alert"


@pytest.mark.parametrize(
    "watts,state,alert",
    [((180.0, 175.0), "ok", False), ((350.0, 0.4), "degraded", True), ((0.0, 0.0), "off", False)],
)
def test_proxmox_pdu_feeds(pve_env, defaults_config, watts, state, alert):
    collector = ProxmoxCollector(defaults_config)  # defaults: PSU 1 = outlet 10, PSU 2 = 16
    pdu = {
        "outlets": {
            10: {"watts": watts[0], "amps": 1.5, "volts": 120.0, "relay_on": True},
            16: {"watts": watts[1], "amps": 1.4, "volts": 120.0, "relay_on": True},
            3: {"watts": 999.0, "amps": 8.0, "volts": 120.0, "relay_on": True},  # not ours
        },
        "site_w": 900.0,
        "budget_w": 1800.0,
        "stale": False,
    }
    payload = collector.shape({"resources": RESOURCES[:1], "pdu": pdu})
    power = payload.stage["power"]
    assert power["state"] == state and power["node"] == "pve1"
    assert [f["label"] for f in power["feeds"]] == ["PSU 1", "PSU 2"]
    assert power["total_w"] == round(sum(watts), 1)
    texts = [t.text for t in payload.tape]
    assert texts[0].endswith(f" · {sum(watts):.0f} W")
    assert any("PSU 2 not drawing" in t for t in texts) is alert


# ---- fantasy ----------------------------------------------------------------------------------


def player(name: str, pid: int, slot: int, actual: float | None, proj: float | None) -> dict:
    stats = []
    if actual is not None:
        stats.append({"statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": actual})
    if proj is not None:
        stats.append({"statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": proj})
    return {
        "lineupSlotId": slot,
        "playerPoolEntry": {"id": pid, "player": {"fullName": name, "id": pid, "stats": stats,
                                                  "defaultPositionId": 1, "proTeamId": 27}},
    }


LEAGUE = {
    "settings": {"name": "Gulf Coast League"},
    "status": {"currentMatchupPeriod": 3, "isActive": True},
    "members": [{"id": "{A}", "displayName": "andy"}, {"id": "{B}", "displayName": "rival"}],
    "teams": [
        {"id": 1, "abbrev": "ANDY", "name": "Andy's Team", "owners": ["{A}"], "playoffSeed": 1,
         "record": {"overall": {"wins": 2, "losses": 0, "pointsFor": 250.4, "pointsAgainst": 190.0}}},
        {"id": 2, "abbrev": "RIV", "location": "Rival", "nickname": "Squad", "owners": ["{B}"],
         "playoffSeed": 2, "record": {"overall": {"wins": 1, "losses": 1, "pointsFor": 210.0,
                                                  "pointsAgainst": 230.0}}},
    ],
    "schedule": [
        {"matchupPeriodId": 1, "winner": "HOME", "home": {"teamId": 1, "totalPoints": 130.2},
         "away": {"teamId": 2, "totalPoints": 101.0}},
        {"matchupPeriodId": 3, "winner": "UNDECIDED",
         "home": {"teamId": 1, "totalPointsLive": 45.5, "totalProjectedPointsLive": 118.0,
                  "rosterForCurrentMatchupPeriod": {"entries": [
                      player("Mike Evans", 16737, 4, 21.5, 16.0),
                      player("Bench Guy", 1, 20, 30.0, 10.0),  # bench slot: not a starter
                  ]}},
         "away": {"teamId": 2, "totalPointsLive": 38.0, "totalProjectedPointsLive": 104.0}},
    ],
}


def test_fantasy_shape(defaults_config):
    defaults_config["modules"]["fantasy"].update(league_id="12345", team_id=1, season=2026)
    collector = FantasyCollector(defaults_config)
    payload = collector.shape(LEAGUE)
    stage = payload.stage
    assert stage["meta"] == {"league": "Gulf Coast League", "leagueId": "12345", "season": 2026,
                             "week": 3, "seasonActive": True}
    matchup = stage["matchup"]
    assert matchup["state"] == "in" and matchup["mineSide"] == "home"
    assert matchup["home"]["points"] == 45.5 and matchup["away"]["name"] == "Rival Squad"
    assert [s["name"] for s in matchup["home"]["starters"]] == ["Mike Evans"]
    wp = matchup["winProbability"]
    assert wp["home_pct"] > 50 and wp["home_pct"] + wp["away_pct"] == pytest.approx(100)
    assert stage["myTeam"]["rank"] == 1 and stage["myTeam"]["owner"] == "andy"
    assert stage["trend"] == [{"week": 1, "points": 130.2, "opponent": "RIV",
                               "opponentPoints": 101.0, "result": "W"}]
    assert collector.interval == collector.live_interval  # my matchup is live
    assert payload.tape[0].text.startswith("FFL: ANDY 45.5 vs RIV 38.0 · ")
    assert payload.tape[0].text.endswith("% win (LIVE)") and payload.tape[0].accent == "up"
    assert payload.tape[1].text == "FFL: ANDY 2-0, #1 · 250 PF"


def test_fantasy_team_resolved_by_name(defaults_config):
    defaults_config["modules"]["fantasy"].update(league_id="1", team_name="rival")
    stage = FantasyCollector(defaults_config).shape(LEAGUE).stage
    assert stage["matchup"]["mineSide"] == "away" and stage["myTeam"]["abbrev"] == "RIV"


def test_fantasy_win_probability():
    assert win_probability(110, 100, 0, 0) == 100.0
    assert win_probability(100, 110, 0, 0) == 0.0
    assert win_probability(100, 100, 0, 0) == 50.0
    assert win_probability(100, 100, 50, 50) == 50.0
    assert 50 < win_probability(110, 100, 60, 60) < 100


async def test_fantasy_without_league_id_fails_before_any_request(defaults_config, http):
    with pytest.raises(RuntimeError, match="league_id"):
        await FantasyCollector(defaults_config).fetch()
    assert http.requests == []


# ---- astro ------------------------------------------------------------------------------------


def test_moon_phase_reference_dates():
    name, illum = moon_phase(datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc))
    assert name == "New moon" and illum < 5
    name, illum = moon_phase(datetime(2000, 1, 21, 4, 40, tzinfo=timezone.utc))
    assert name == "Full moon" and illum > 95
