"""backend/collectors/sports.py: the per-day schedule window (ESPN rejects date
ranges), parsing/shaping of recorded ESPN scoreboards, and score celebrations."""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from backend.collectors import sports as sports_module
from backend.collectors.sports import (
    SportsCollector,
    _american_to_implied,
    _prob_from_moneyline,
    latest_scoring_play,
)
from helpers import FakeBus, json_response, load_json

NFL = {"sport": "football", "league": "nfl"}
MLB = {"sport": "baseball", "league": "mlb"}
FIXTURE_FILES = {
    "espn_nfl_20260924.json": NFL,  # ATL @ GB final
    "espn_nfl_20260927.json": NFL,  # MIN @ TB (followed), LAC @ BUF, KC @ MIA — pre, with odds
    "espn_mlb_20260924.json": MLB,  # TB @ NYY (followed), STL @ PIT, SD @ LAD — finals
    "espn_mlb_20260925.json": MLB,  # TB @ PHI (followed), CHC @ BOS, LAD @ SF — pre, probables
}


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


class Espn:
    """Fake ESPN scoreboard: one day per request, like the real API since Sep 2026."""

    def __init__(self) -> None:
        today = date.today()
        self.today = today
        self.by_day = {
            ("nfl", today): "espn_nfl_20260924.json",
            ("nfl", today + timedelta(days=1)): "espn_nfl_20260927.json",
            ("mlb", today - timedelta(days=1)): "espn_mlb_20260924.json",
            ("mlb", today): "espn_mlb_20260925.json",
        }
        self.undated = {"nfl": "espn_nfl_20260924.json", "mlb": "espn_mlb_20260925.json"}
        self.fail_days: set[date] = set()
        self.fail_undated: set[str] = set()
        self.requests: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/scoreboard"):
            return httpx.Response(404)  # summary / core API: not recorded
        league = request.url.path.split("/")[-2]
        dates = request.url.params.get("dates")
        self.requests.append((league, dates))
        if dates is None:
            if league in self.fail_undated:
                return httpx.Response(503)
            return json_response(load_json(self.undated[league]))
        if not re.fullmatch(r"\d{8}", dates):
            return json_response({"code": 400, "message": "Failed to get events"}, status=400)
        day = datetime.strptime(dates, "%Y%m%d").date()
        if day in self.fail_days:
            return json_response({"code": 400}, status=400)
        name = self.by_day.get((league, day))
        return json_response(load_json(name) if name else {"events": []})

    def dated(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[1] is not None]


@pytest.fixture
def config(defaults_config) -> dict:
    sports = defaults_config["modules"]["sports"]
    sports.update(leagues=[NFL, MLB], days_back=2, days_ahead=1)
    return defaults_config


@pytest.fixture
def shape_config(config) -> dict:
    # shape() drops upcoming games past now + days_ahead; every recorded
    # kickoff is within 7 days of the recording date, so from then on all fit.
    config["modules"]["sports"]["days_ahead"] = 7
    return config


def all_fixture_ids() -> set[str]:
    return {e["id"] for name in FIXTURE_FILES for e in load_json(name)["events"]}


def fixture_games(collector: SportsCollector) -> list[dict]:
    games = []
    for name, league in FIXTURE_FILES.items():
        for event in load_json(name)["events"]:
            games.append(collector._parse_event(event, league))
    return games


async def refresh(collector: SportsCollector, espn: Espn) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(espn)) as client:
        await collector._refresh_window(client)


# ---- schedule window ----------------------------------------------------------------


async def test_window_requests_one_single_date_per_league_per_day(config):
    espn, collector = Espn(), SportsCollector(config)
    await refresh(collector, espn)
    days = [espn.today + timedelta(days=d) for d in range(-2, 2)]
    assert sorted(espn.requests) == sorted((lg, ymd(d)) for lg in ("nfl", "mlb") for d in days)
    assert all(re.fullmatch(r"\d{8}", d) for _, d in espn.requests)  # never a range
    assert collector.window_status | {"checked_at": None} == {
        "checked_at": None, "days": 4, "fetched": 8, "failed": 0, "error": None,
    }
    assert {g["id"] for g in collector._window} == all_fixture_ids()
    assert collector._window_at > 0 and collector._window_day == espn.today.isoformat()


async def test_window_is_not_refetched_while_fresh(config):
    espn, collector = Espn(), SportsCollector(config)
    await refresh(collector, espn)
    await refresh(collector, espn)
    assert len(espn.requests) == 8


async def test_settled_days_are_cached(config):
    espn, collector = Espn(), SportsCollector(config)
    await refresh(collector, espn)
    espn.requests.clear()
    collector._window_at = 0.0  # schedule interval elapsed
    await refresh(collector, espn)
    # today-2 ended before yesterday: settled, served from cache.
    refetched = {d for _, d in espn.requests}
    assert refetched == {ymd(espn.today + timedelta(days=d)) for d in (-1, 0, 1)}
    assert len(espn.requests) == 6
    assert collector.window_status["fetched"] == 6
    assert {g["id"] for g in collector._window} == all_fixture_ids()


async def test_settled_day_that_failed_is_retried(config):
    espn, collector = Espn(), SportsCollector(config)
    settled = espn.today - timedelta(days=2)
    espn.fail_days = {settled}
    await refresh(collector, espn)
    espn.fail_days = set()
    espn.requests.clear()
    collector._window_at = 0.0
    await refresh(collector, espn)
    assert ("nfl", ymd(settled)) in espn.requests and ("mlb", ymd(settled)) in espn.requests


async def test_partial_failure_is_recorded(config):
    espn, collector = Espn(), SportsCollector(config)
    bad = espn.today + timedelta(days=1)
    espn.fail_days = {bad}
    await refresh(collector, espn)
    status = collector.window_status
    assert (status["fetched"], status["failed"]) == (6, 2)
    assert status["error"].startswith(f"football/nfl {bad.isoformat()}: HTTPStatusError") or \
        status["error"].startswith(f"baseball/mlb {bad.isoformat()}: HTTPStatusError")
    nfl_sunday = {e["id"] for e in load_json("espn_nfl_20260927.json")["events"]}
    window_ids = {g["id"] for g in collector._window}
    assert window_ids == all_fixture_ids() - nfl_sunday
    assert collector._window_at > 0  # the rest of the window is usable


async def test_failed_day_keeps_its_cached_copy(config):
    espn, collector = Espn(), SportsCollector(config)
    await refresh(collector, espn)
    espn.fail_days = {espn.today}
    collector._window_at = 0.0
    await refresh(collector, espn)
    assert collector.window_status["failed"] == 2
    assert {g["id"] for g in collector._window} == all_fixture_ids()


async def test_total_failure_without_cache_retries_next_poll(config):
    espn, collector = Espn(), SportsCollector(config)
    espn.fail_days = {espn.today + timedelta(days=d) for d in range(-2, 2)}
    await refresh(collector, espn)
    assert collector.window_status["failed"] == 8 and collector.window_status["fetched"] == 0
    assert collector._window == [] and collector._window_at == 0.0
    espn.fail_days = set()
    await refresh(collector, espn)  # not "fresh": tries again straight away
    assert collector.window_status["fetched"] == 8


async def test_window_drops_days_that_scroll_out(config):
    espn, collector = Espn(), SportsCollector(config)
    await refresh(collector, espn)
    stale_key = ("football/nfl", espn.today - timedelta(days=30))
    collector._window_days[stale_key] = [{"id": "ancient"}]
    collector._window_at = 0.0
    await refresh(collector, espn)
    assert stale_key not in collector._window_days
    assert "ancient" not in {g["id"] for g in collector._window}


async def test_fetch_reports_window_degradation(http, config):
    espn = Espn()
    espn.fail_days = {espn.today}
    http.handler = espn
    collector = SportsCollector(config)
    games = await collector.fetch()
    assert collector.degraded == "schedule window: 2/8 days failing"
    assert all(entry["ok"] for entry in collector.league_status.values())
    assert {g["id"] for g in games} >= {"401872948", "401817078"}  # fast lane still merged in
    status = collector.status()
    assert status["window"]["failed"] == 2 and len(status["leagues"]) == 2


async def test_fetch_one_league_down(http, config):
    espn = Espn()
    espn.fail_undated = {"mlb"}
    http.handler = espn
    collector = SportsCollector(config)
    await collector.fetch()
    assert collector.degraded == "1/2 league scoreboards failing"
    assert collector.league_status["baseball/mlb"]["ok"] is False
    assert collector.league_status["football/nfl"]["games"] == 1


async def test_fetch_all_leagues_down_raises(http, config):
    espn = Espn()
    espn.fail_undated = {"nfl", "mlb"}
    http.handler = espn
    with pytest.raises(RuntimeError, match="all league scoreboards failed"):
        await SportsCollector(config).fetch()


def test_merge_window_prefers_fresh_scores_but_keeps_window_only_fields(config):
    collector = SportsCollector(config)
    window_game = collector._parse_event(load_json("espn_mlb_20260925.json")["events"][0], MLB)
    assert window_game["odds"] and window_game["home"]["probable"]
    live = copy.deepcopy(window_game)
    live.update(state="in", odds=None)
    live["home"].update(score="3", probable=None, records={})
    collector._window = [window_game]
    merged = collector._merge_window([live])
    assert len(merged) == 1
    game = merged[0]
    assert game["state"] == "in" and game["home"]["score"] == "3"
    assert game["odds"] == window_game["odds"]
    assert game["home"]["probable"] == window_game["home"]["probable"]
    assert game["home"]["records"] == window_game["home"]["records"]


# ---- parsing + shape against the recordings ------------------------------------------


def test_parse_events(config):
    collector = SportsCollector(config)
    games = {g["id"]: g for g in fixture_games(collector)}
    assert len(games) == 10 and None not in games

    final = games["401817064"]  # TB 4 @ NYY 6
    assert (final["state"], final["league"], final["sport"]) == ("post", "MLB", "baseball")
    assert (final["away"]["abbrev"], final["away"]["score"], final["home"]["score"]) == ("TB", "4", "6")
    assert final["followed"] is True and final["followed_side"] == "away"
    assert final["away"]["linescores"][:3] == [0.0, 0.0, 2.0]
    assert final["away"]["records"]["overall"] == final["away"]["record"]

    pre = games["401817078"]  # TB @ PHI, probables + moneyline
    assert pre["state"] == "pre" and pre["odds"]["details"] == "PHI -171"
    assert pre["win_prob"]["source"] == "moneyline"
    assert pre["win_prob"]["home"] + pre["win_prob"]["away"] == pytest.approx(100, abs=0.2)
    assert pre["win_prob"]["home"] > 50  # PHI favoured
    assert pre["home"]["probable"]["name"] == "C. Sanchez"  # PHI starter
    assert "ERA" in pre["home"]["probable"]["stat"]

    bucs = games["401872959"]  # MIN @ TB
    assert bucs["followed_side"] == "home" and bucs["sport"] == "football"
    assert bucs["odds"]["details"] == "MIN -1.5"
    assert games["401872948"]["followed"] is False  # ATL @ GB


def test_parse_honours_display_toggles(config):
    config["modules"]["sports"].update(show_odds=False, show_records=False, win_probability=False)
    collector = SportsCollector(config)
    for game in fixture_games(collector):
        assert game["odds"] is None and game["win_prob"] is None
        assert game["home"]["records"] == {} and game["away"]["records"] == {}


def test_parse_rejects_events_without_both_sides(config):
    collector = SportsCollector(config)
    event = copy.deepcopy(load_json("espn_nfl_20260924.json")["events"][0])
    event["competitions"][0]["competitors"] = event["competitions"][0]["competitors"][:1]
    assert collector._parse_event(event, NFL) is None
    assert collector._parse_event({"competitions": "garbage"}, NFL) is None


def test_shape_buckets_order_and_tape(shape_config):
    config = shape_config
    collector = SportsCollector(config)
    payload = collector.shape(fixture_games(collector))
    games = payload.stage["games"]
    buckets = [g["bucket"] for g in games]
    assert buckets == ["recent"] * 4 + ["next"] * 6
    recent = [g["start"] for g in games if g["bucket"] == "recent"]
    upcoming = [g["start"] for g in games if g["bucket"] == "next"]
    assert recent == sorted(recent, reverse=True) and upcoming == sorted(upcoming)
    assert payload.stage["next_up"] == "401817078"  # first followed upcoming game
    assert collector.interval == collector.idle_interval  # nothing live
    texts = [t.text for t in payload.tape]
    assert "TB 4 – NYY 6 (F)" in texts
    assert any(t.startswith("TB at PHI · ") and t.endswith(" · PHI -171") for t in texts)
    followed = {t.text for t in payload.tape if t.priority == 1}
    assert len(followed) == 3  # TB@NYY, TB@PHI, MIN@TB
    assert {t.icon for t in payload.tape} == {"baseball", "football"}


def test_shape_reserves_a_seat_for_followed_teams(shape_config):
    config = shape_config
    config["modules"]["sports"].update(max_recent=1, max_upcoming=1)
    collector = SportsCollector(config)
    payload = collector.shape(fixture_games(collector))
    ids = [g["id"] for g in payload.stage["games"]]
    # SD @ LAD is the newest final, but the Rays' final keeps its seat; the
    # cap is enforced after the reserved seats.
    assert ids == ["401817064", "401817078"]


def test_shape_live_game_tightens_the_poll(shape_config):
    config = shape_config
    collector = SportsCollector(config)
    games = fixture_games(collector)
    live = next(g for g in games if g["id"] == "401817078")
    live.update(state="in", detail="Top 3rd")
    live["away"]["score"], live["home"]["score"] = "1", "0"
    payload = collector.shape(games)
    assert collector.interval == collector.live_interval
    assert live["bucket"] == "live"
    item = next(t for t in payload.tape if t.text.startswith("TB 1 – PHI 0"))
    assert item.text == "TB 1 – PHI 0 (Top 3rd)" and item.accent == "alert"


def test_shape_drops_games_beyond_the_horizon(shape_config):
    collector = SportsCollector(shape_config)
    games = fixture_games(collector)
    far = next(g for g in games if g["id"] == "401817086")
    far["start"] = (datetime.now().astimezone() + timedelta(days=30)).isoformat()
    ids = [g["id"] for g in collector.shape(games).stage["games"]]
    assert "401817086" not in ids and len(ids) == 9


# ---- odds helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "odds,expected",
    [(-150, 0.6), ("+150", 0.4), (100, 0.5), ("EVEN", None), (None, None), ("", None), ("abc", None), (0, None)],
)
def test_american_to_implied(odds, expected):
    result = _american_to_implied(odds)
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_prob_from_moneyline_removes_the_vig():
    prob = _prob_from_moneyline({"moneyline": {"home": "-120", "away": "+100"}})
    assert prob["home"] + prob["away"] == pytest.approx(100, abs=0.1)
    assert prob["home"] > prob["away"] and prob["source"] == "moneyline"
    assert _prob_from_moneyline({"moneyline": {"home": "-120"}}) is None
    assert _prob_from_moneyline(None) is None


# ---- celebrations -----------------------------------------------------------------------


def live_game(sport: str, gid: str, away: int, home: int, followed_side: str = "home") -> dict:
    teams = {"away": "Visitors", "home": "Visitors"}
    teams[followed_side] = {"baseball": "Tampa Bay Rays", "basketball": "Tampa Bay Rays",
                            "hockey": "Tampa Bay Lightning"}[sport]
    return {
        "id": gid,
        "sport": sport,
        "league": "MLB",
        "state": "in",
        "followed": True,
        "away": {"abbrev": "VIS", "id": "1", "name": teams["away"], "score": str(away)},
        "home": {"abbrev": "TB", "id": "30", "name": teams["home"], "score": str(home)},
    }


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(sports_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


async def celebrate(collector, games) -> None:
    await collector._maybe_celebrate(games)


async def test_run_celebrates_with_offline_summary(http, config, clock):
    http.handler = lambda request: httpx.Response(404)  # summary unavailable
    bus = FakeBus()
    collector = SportsCollector(config)
    collector._bus = bus
    await celebrate(collector, [live_game("baseball", "g1", 0, 0)])  # first poll seeds only
    assert bus.messages == []
    clock.now += 30
    await celebrate(collector, [live_game("baseball", "g1", 0, 1)])
    [message] = bus.of_type("sport_event")
    event = message["event"]
    assert event["label"] == "RUN SCORED" and event["team"]["abbrev"] == "TB"
    assert event["team_is_home"] is True and event["scorer"] is None
    assert "/summary" in str(http.requests[0].url)


async def test_sport_aware_cooldown(http, config, clock):
    bus = FakeBus()
    collector = SportsCollector(config)
    collector._bus = bus
    games = lambda b, k: [live_game("baseball", "mlb1", 0, b), live_game("basketball", "nba1", 0, k)]  # noqa: E731
    await celebrate(collector, games(0, 0))
    for step in range(1, 4):  # a score on every poll, 30 s apart
        clock.now += 30
        await celebrate(collector, games(step, step * 2))
    sports = [m["event"]["sport"] for m in bus.of_type("sport_event")]
    assert sports.count("baseball") == 3  # every run fires (20 s cooldown)
    assert sports.count("basketball") == 1  # throttled (180 s cooldown)


async def test_opponent_scores_and_idle_games_do_not_celebrate(http, config, clock):
    bus = FakeBus()
    collector = SportsCollector(config)
    collector._bus = bus
    await celebrate(collector, [live_game("hockey", "h1", 0, 0)])
    clock.now += 60
    await celebrate(collector, [live_game("hockey", "h1", 2, 0)])  # opponent scored
    final = live_game("hockey", "h1", 2, 1)
    final["state"] = "post"
    clock.now += 60
    await celebrate(collector, [final])
    assert bus.of_type("sport_event") == []


async def test_celebrations_can_be_disabled(http, config, clock):
    config["modules"]["sports"]["celebrations"] = False
    bus = FakeBus()
    collector = SportsCollector(config)
    collector._bus = bus
    await celebrate(collector, [live_game("baseball", "g1", 0, 0)])
    clock.now += 60
    await celebrate(collector, [live_game("baseball", "g1", 0, 5)])
    assert bus.messages == [] and http.requests == []


def test_mlb_scoring_play_matched_by_team_id():
    # MLB plays carry only {"team": {"id": ...}}; the last scoring play overall
    # is the opponent's, and must not be picked for the followed team.
    summary = {
        "plays": [
            {"id": "1", "scoringPlay": True, "team": {"id": "30"}, "text": "Lowe homers"},
            {"id": "2", "scoringPlay": False, "team": {"id": "30"}, "text": "Lowe grounds out"},
            {"id": "3", "scoringPlay": True, "team": {"id": "10"}, "text": "Judge homers"},
        ]
    }
    assert latest_scoring_play(summary, "TB", team_id="30")["id"] == "1"
    assert latest_scoring_play(summary, "TB")["id"] == "3"  # no id: falls back to any team


def test_football_prefers_touchdowns():
    summary = {
        "scoringPlays": [
            {"id": "td", "type": {"text": "Passing Touchdown"}, "team": {"abbreviation": "TB"}},
            {"id": "pat", "type": {"text": "Extra Point Good"}, "team": {"abbreviation": "TB"}},
        ]
    }
    assert latest_scoring_play(summary, "TB")["id"] == "pat"
    assert latest_scoring_play(summary, "TB", prefer_touchdown=True)["id"] == "td"
