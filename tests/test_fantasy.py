"""backend/collectors/fantasy.py: when a matchup is live (an NFL game one of its
starters plays in is on, not merely points on the board), the NFL-schedule
lookup's health, and the collector's lifecycle (celebration tasks, its client).
Shaped from hand-written inputs: the ESPN fantasy API is never contacted."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.collectors import fantasy as fantasy_module
from backend.collectors.fantasy import FantasyCollector
from helpers import FakeBus, until

UTC = timezone.utc
THURSDAY = datetime(2026, 9, 24, 20, 15, tzinfo=UTC)  # TB vs ATL
SUNDAY_EARLY = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)  # GB
SUNDAY_LATE = datetime(2026, 9, 27, 20, 25, tzinfo=UTC)  # KC
TB, ATL, GB, KC = 27, 1, 9, 12


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def pro_game(opp: int, home: bool, when: datetime) -> dict:
    return {"oppId": opp, "oppAbbrev": "", "home": home, "date": ms(when)}


PRO = {  # pro_teams() output, scoring period 3
    TB: {"abbrev": "TB", "byeWeek": 11, "games": {3: pro_game(ATL, True, THURSDAY)}},
    ATL: {"abbrev": "ATL", "byeWeek": 5, "games": {3: pro_game(TB, False, THURSDAY)}},
    GB: {"abbrev": "GB", "byeWeek": 7, "games": {3: pro_game(30, True, SUNDAY_EARLY)}},
    KC: {"abbrev": "KC", "byeWeek": 10, "games": {3: pro_game(31, False, SUNDAY_LATE)}},
}


def starter(name: str, pid: int, team: int, actual: float | None, proj: float = 12.0) -> dict:
    stats = [{"statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": proj}]
    if actual is not None:
        stats.append({"statSourceId": 0, "statSplitTypeId": 1, "appliedTotal": actual})
    return {
        "lineupSlotId": 4,
        "playerPoolEntry": {"id": pid, "player": {"fullName": name, "id": pid, "stats": stats,
                                                  "defaultPositionId": 3, "proTeamId": team}},
    }


def side(team_id: int, points: float, roster: list[dict] | None) -> dict:
    out = {"teamId": team_id, "totalPointsLive": points, "totalProjectedPointsLive": points + 80}
    if roster is not None:
        out["rosterForCurrentMatchupPeriod"] = {"entries": roster}
    return out


def league(rosters: bool = True, my_points: float = 24.5) -> dict:
    """Week 3 after Thursday night: my TB receiver has scored, my GB starter
    plays Sunday early; the other matchup's starters all play Sunday late."""
    return {
        "settings": {"name": "Gulf Coast League"},
        "scoringPeriodId": 3,
        "status": {"currentMatchupPeriod": 3, "isActive": True},
        "members": [],
        "teams": [{"id": i, "abbrev": a, "name": a} for i, a in
                  ((1, "ANDY"), (2, "RIV"), (3, "THR"), (4, "FOU"))],
        "schedule": [
            {"matchupPeriodId": 3, "winner": "UNDECIDED",
             "home": side(1, my_points, [starter("Mike Evans", 16737, TB, my_points),
                                         starter("Jayden Reed", 4429, GB, None)] if rosters else None),
             "away": side(2, 8.0, [starter("Bijan Robinson", 4430, ATL, 8.0)] if rosters else None)},
            {"matchupPeriodId": 3, "winner": "UNDECIDED",
             "home": side(3, 0.0, [starter("Travis Kelce", 15847, KC, None)] if rosters else None),
             "away": side(4, 0.0, [starter("Isiah Pacheco", 4361, KC, None)] if rosters else None)},
        ],
    }


@pytest.fixture(autouse=True)
def _fresh_pro_state(monkeypatch):
    """The NFL schedule cache and its errors live at module level (shared
    with the API) — isolate them per test."""
    monkeypatch.setattr(fantasy_module, "_pro_cache", {})
    monkeypatch.setattr(fantasy_module, "_pro_errors", {}, raising=False)


@pytest.fixture
def config(defaults_config) -> dict:
    defaults_config["modules"]["fantasy"].update(league_id="12345", team_id=1, season=2026)
    return defaults_config


@pytest.fixture
def at(monkeypatch):
    def set_now(when: datetime) -> None:
        monkeypatch.setattr(fantasy_module, "_now", lambda: when)
    return set_now


def shaped(config, when_fn, when: datetime, raw: dict | None = None):
    when_fn(when)
    collector = FantasyCollector(config)
    collector._pro = PRO
    payload = collector.shape(raw or league())
    return collector, payload


# ---- live means a starter's NFL game is on ---------------------------------------


def test_matchup_is_live_while_a_starters_game_is_on(config, at):
    collector, payload = shaped(config, at, THURSDAY + timedelta(hours=1))
    mine, other = payload.stage["scoreboard"]
    assert payload.stage["matchup"] is mine
    assert mine["state"] == "in" and other["state"] == "pre"  # theirs play Sunday
    assert collector.interval == collector.live_interval
    assert payload.tape[0].text.endswith("(LIVE)")


@pytest.mark.parametrize("when", [
    THURSDAY + timedelta(hours=5),  # Thursday's game over, points on the board
    datetime(2026, 9, 25, 16, 0, tzinfo=UTC),  # Friday
    datetime(2026, 9, 29, 12, 0, tzinfo=UTC),  # Tuesday morning, not yet settled
])
def test_points_on_the_board_between_games_are_not_live(config, at, when):
    collector, payload = shaped(config, at, when)
    matchup = payload.stage["matchup"]
    assert matchup["home"]["points"] == 24.5 and matchup["winner"] == "UNDECIDED"
    # Was "in" from Thursday night to Tuesday: 30 s polling and the display
    # pinned (main.ts AUTO_RULES: matchup.state === "in") for 4.5 days.
    assert matchup["state"] == "pre"
    assert collector.interval == collector.idle_interval
    assert "(LIVE)" not in payload.tape[0].text


def test_sunday_slate_is_live_per_matchup(config, at):
    _, payload = shaped(config, at, SUNDAY_EARLY + timedelta(hours=2))
    assert [m["state"] for m in payload.stage["scoreboard"]] == ["in", "pre"]
    _, payload = shaped(config, at, SUNDAY_LATE + timedelta(minutes=5))
    assert [m["state"] for m in payload.stage["scoreboard"]] == ["in", "in"]  # GB's still on
    _, payload = shaped(config, at, SUNDAY_EARLY + timedelta(hours=4, minutes=1))
    assert [m["state"] for m in payload.stage["scoreboard"]] == ["pre", "in"]


def test_idle_poll_wakes_for_the_next_kickoff(config, at):
    collector, payload = shaped(config, at, SUNDAY_EARLY - timedelta(minutes=10))
    assert payload.stage["matchup"]["state"] == "pre"
    assert collector.interval == pytest.approx(601)  # not 30 min past kickoff


def test_without_rosters_any_nfl_game_counts(config, at):
    _, payload = shaped(config, at, SUNDAY_LATE + timedelta(hours=1), league(rosters=False))
    assert [m["state"] for m in payload.stage["scoreboard"]] == ["in", "in"]
    _, payload = shaped(config, at, datetime(2026, 9, 25, 16, 0, tzinfo=UTC), league(rosters=False))
    assert [m["state"] for m in payload.stage["scoreboard"]] == ["pre", "pre"]


def test_without_a_schedule_points_still_mean_live(config, at):
    at(datetime(2026, 9, 25, 16, 0, tzinfo=UTC))
    collector = FantasyCollector(config)  # pro_teams never answered: _pro == {}
    payload = collector.shape(league())
    assert payload.stage["matchup"]["state"] == "in"


# ---- the NFL schedule lookup is reported, not just logged -------------------------


PRO_SCHEDULE = {"settings": {"proTeams": [
    {"id": TB, "abbrev": "tb", "byeWeek": 11,
     "proGamesByScoringPeriod": {"3": [{"homeProTeamId": TB, "awayProTeamId": ATL, "date": ms(THURSDAY)}]}},
    {"id": ATL, "abbrev": "atl", "byeWeek": 5,
     "proGamesByScoringPeriod": {"3": [{"homeProTeamId": TB, "awayProTeamId": ATL, "date": ms(THURSDAY)}]}},
]}}


class Espn:
    def __init__(self) -> None:
        self.schedule: httpx.Response | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "/segments/" in request.url.path:
            return httpx.Response(200, json=league())
        return self.schedule or httpx.Response(200, json=PRO_SCHEDULE)


async def test_schedule_failure_is_degraded_until_it_recovers(http, config):
    espn = Espn()
    espn.schedule = httpx.Response(503)
    http.handler = espn
    collector = FantasyCollector(config)
    await collector.fetch()
    assert collector.degraded.startswith("NFL schedule: HTTPStatusError")
    espn.schedule = None
    await collector.fetch()  # a failure isn't cached: retried, and clears
    assert collector.degraded is None
    assert collector._pro[TB]["games"][3]["oppAbbrev"] == "ATL"


async def test_unparseable_schedule_degrades_instead_of_failing_the_poll(http, config):
    espn = Espn()
    espn.schedule = httpx.Response(200, json={"settings": {"proTeams": [
        {"id": TB, "proGamesByScoringPeriod": {"week three": [{}]}},
    ]}})
    http.handler = espn
    collector = FantasyCollector(config)
    raw = await collector.fetch()
    assert raw["settings"]["name"] == "Gulf Coast League"
    assert collector.degraded.startswith("NFL schedule: ValueError")


async def test_empty_schedule_is_degraded(http, config):
    espn = Espn()
    espn.schedule = httpx.Response(200, json={})
    http.handler = espn
    collector = FantasyCollector(config)
    await collector.fetch()
    assert collector.degraded == "NFL schedule: empty"


# ---- lifecycle -------------------------------------------------------------------------


async def test_celebration_broadcast_is_held_until_it_runs(config, at, monkeypatch):
    monkeypatch.setattr(fantasy_module, "time", SimpleNamespace(monotonic=lambda: 10_000.0))
    at(THURSDAY + timedelta(hours=1))
    bus = FakeBus()
    collector = FantasyCollector(config)
    collector._bus = bus
    collector._pro = PRO
    collector.shape(league(my_points=20.0))  # seeds
    collector.shape(league(my_points=26.0))
    # asyncio keeps only weak references to tasks: an unreferenced broadcast
    # could be collected before it ran.
    [task] = collector._event_tasks
    await task
    assert collector._event_tasks == set()
    [message] = bus.of_type("fantasy_event")
    assert message["event"]["text"].startswith("+6.0 pts")


async def test_client_is_closed_when_the_collector_stops(http, config):
    http.handler = Espn()
    collector = FantasyCollector(config)
    task = asyncio.create_task(collector.start(FakeBus()))
    await until(lambda: collector.last_success is not None)
    client = collector._client
    assert client is not None and not client.is_closed
    task.cancel()  # what CollectorManager does on a config save
    await asyncio.gather(task, return_exceptions=True)
    assert client.is_closed
