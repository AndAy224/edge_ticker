"""Sports collector — ESPN public scoreboard endpoints (unofficial).

All parsing is isolated in shape()/_parse_event so an upstream format change
degrades to a stale module, never a crash. Poll rate tightens automatically
while any followed-league game is live.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from ..state import Bus, ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary"
SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team}/schedule"
# ESPN's "core" API. The site summary endpoint carries the same win-probability
# numbers, but a finished MLB summary is ~1MB (every play, every at-bat) — far
# too heavy to poll. These resources are 1-4KB.
CORE_URL = (
    "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}"
    "/events/{event}/competitions/{event}/{resource}"
)

# Followed-team score celebrations: minimum gap between a team's celebrations.
# The score-diff only fires on an *increase*, so for discrete-scoring sports (a
# run, a goal) a short gap just dedupes within a poll — every score celebrates.
# Basketball scores nearly every poll, so it stays throttled; football groups a
# touchdown with its trailing extra-point / two-point try.
CELEBRATION_COOLDOWN_SECONDS = 20.0
SPORT_COOLDOWN_SECONDS = {"basketball": 180.0, "football": 90.0}

# Per-day window fetches in flight at once (per poll, across all leagues).
WINDOW_CONCURRENCY = 4

FOOTBALL_DELTA_LABELS = {6: "TOUCHDOWN", 3: "FIELD GOAL", 2: "TWO-POINT", 1: "EXTRA POINT"}


# ---- ESPN summary extraction (shared by the live path and the admin test) ----


def _label_from_type(type_text: str) -> str | None:
    t = type_text.lower()
    if "touchdown" in t:
        return "TOUCHDOWN"
    if "field goal" in t:
        return "FIELD GOAL"
    if "home run" in t:
        return "HOME RUN"
    if "goal" in t:
        return "GOAL"
    return None


def _delta_label(sport: str | None, delta: int, text: str) -> str:
    if sport == "football":
        return FOOTBALL_DELTA_LABELS.get(delta, "SCORE")
    if sport == "hockey":
        return "GOAL"
    if sport == "baseball":
        return "HOME RUN" if "homer" in text.lower() else "RUN SCORED"
    return "SCORE"


def _boxscore_athletes(summary: dict) -> list[dict]:
    athletes = []
    for team in summary.get("boxscore", {}).get("players", []):
        for stat in team.get("statistics", []):
            for entry in stat.get("athletes", []):
                ath = entry.get("athlete") or {}
                if ath.get("displayName"):
                    athletes.append(ath)
    return athletes


def _scorer_from_participants(summary: dict, play: dict) -> dict | None:
    """MLB-style plays carry participant athlete ids; resolve via the boxscore."""
    by_id = {str(a.get("id")): a for a in _boxscore_athletes(summary)}
    participants = play.get("participants") or []
    for wanted in ("scorer", "batter", "rusher", "receiver"):
        for p in participants:
            if p.get("type") == wanted:
                ath = by_id.get(str((p.get("athlete") or {}).get("id")))
                if ath:
                    return {
                        "name": ath.get("displayName"),
                        "headshot": (ath.get("headshot") or {}).get("href"),
                    }
    return None


def _scorer_from_text(summary: dict, text: str) -> dict | None:
    """NFL-style scoring plays have no participants — match boxscore names
    against the play text; the earliest mention is the scorer ("Golden 23 Yd
    pass from Love" → Golden, not Love)."""
    best: dict | None = None
    best_pos = len(text) + 1
    for ath in _boxscore_athletes(summary):
        name = ath["displayName"]
        pos = text.find(name)
        if pos < 0:
            pos = text.find(name.split()[-1])
        if 0 <= pos < best_pos:
            best_pos = pos
            best = {
                "name": name,
                "headshot": (ath.get("headshot") or {}).get("href"),
            }
    return best


def latest_scoring_play(
    summary: dict,
    team_abbrev: str | None = None,
    prefer_touchdown: bool = False,
    team_id: str | None = None,
) -> dict | None:
    plays = summary.get("scoringPlays") or [
        p for p in summary.get("plays", []) if p.get("scoringPlay")
    ]
    if team_abbrev or team_id:
        # Football/hockey scoring plays carry the team abbreviation; MLB plays
        # carry only a numeric team id ({"team": {"id": "30"}}) — match either,
        # so the celebration shows the followed team's play, not the opponent's.
        team_plays = [
            p
            for p in plays
            if (team_abbrev and (p.get("team") or {}).get("abbreviation") == team_abbrev)
            or (team_id is not None and str((p.get("team") or {}).get("id")) == str(team_id))
        ]
        plays = team_plays or plays
    if prefer_touchdown:
        tds = [
            p
            for p in plays
            if "touchdown" in ((p.get("type") or {}).get("text") or "").lower()
        ]
        plays = tds or plays
    return plays[-1] if plays else None


async def build_test_event() -> dict:
    """Admin test: celebrate the last touchdown from the latest Packers game
    of last season — real play text, scorer headshot, logos."""
    season = datetime.now(timezone.utc).year - 1
    async with httpx.AsyncClient(timeout=20) as client:
        event_id = None
        for seasontype in ("3", "2"):  # postseason first, then regular season
            response = await client.get(
                SCHEDULE_URL.format(sport="football", league="nfl", team="gb"),
                params={"season": str(season), "seasontype": seasontype},
            )
            response.raise_for_status()
            completed = [
                e
                for e in response.json().get("events", [])
                if (e.get("competitions") or [{}])[0]
                .get("status", {})
                .get("type", {})
                .get("completed")
            ]
            if completed:
                event_id = completed[-1]["id"]
                break
        if event_id is None:
            raise RuntimeError("no completed Packers game found for last season")
        response = await client.get(
            SUMMARY_URL.format(sport="football", league="nfl"),
            params={"event": event_id},
        )
        response.raise_for_status()
        summary = response.json()

    play = latest_scoring_play(summary, "GB", prefer_touchdown=True)
    if play is None:
        raise RuntimeError("no Packers scoring play in the test game")
    text = play.get("text", "")
    sides: dict[str, dict] = {}
    for competitor in (
        summary.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    ):
        team = competitor.get("team", {})
        sides[competitor.get("homeAway", "home")] = {
            "abbrev": team.get("abbreviation", "?"),
            "name": team.get("displayName", "?"),
            "color": team.get("color"),
            "logo": ((team.get("logos") or [{}])[0]).get("href"),
        }
    team_is_home = sides.get("home", {}).get("abbrev") == "GB"
    team = sides["home" if team_is_home else "away"]
    opponent = sides["away" if team_is_home else "home"]
    return {
        "sport": "football",
        "league": "NFL",
        "label": _label_from_type((play.get("type") or {}).get("text") or "")
        or "TOUCHDOWN",
        "text": text,
        "team": team,
        "opponent": opponent,
        "away_score": play.get("awayScore"),
        "home_score": play.get("homeScore"),
        "team_is_home": team_is_home,
        "scorer": _scorer_from_text(summary, text),
    }


def _american_to_implied(odds: str | int | float | None) -> float | None:
    """American moneyline -> implied win probability (0..1), vig included."""
    if odds in (None, "", "EVEN", "even"):
        return None
    try:
        value = float(str(odds).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    if value < 0:
        return -value / (-value + 100.0)
    return 100.0 / (value + 100.0)


def _moneyline(side: dict | None) -> str | None:
    """Pull a price off ESPN's moneyline block, freshest variant first."""
    if not isinstance(side, dict):
        return None
    for key in ("current", "close", "open"):
        entry = side.get(key)
        if isinstance(entry, dict) and entry.get("odds") not in (None, ""):
            return entry["odds"]
    # Some payloads inline the price instead of nesting it under open/close.
    return side.get("odds")


def _parse_odds(competition: dict) -> dict | None:
    """Spread / over-under / moneylines from the scoreboard's own odds block.

    Free — the scoreboard response already carries it. ESPN populates it for
    NFL/CFB well ahead of kickoff and for MLB roughly a day out; absent
    entirely for some leagues, so every consumer must tolerate None.
    """
    entries = competition.get("odds") or []
    if not entries:
        return None
    entry = entries[0] or {}
    money = entry.get("moneyline") or {}
    home_ml = _moneyline(money.get("home"))
    away_ml = _moneyline(money.get("away"))
    favorite = None
    for side in ("home", "away"):
        if ((entry.get(f"{side}TeamOdds") or {}).get("favorite")) is True:
            favorite = side
    odds = {
        "details": entry.get("details"),
        "over_under": entry.get("overUnder"),
        "spread": entry.get("spread"),
        "favorite_side": favorite,
        "moneyline": {"home": home_ml, "away": away_ml},
        "provider": (entry.get("provider") or {}).get("name"),
    }
    if not any(
        (odds["details"], odds["over_under"], home_ml, away_ml)
    ):
        return None
    return odds


def _prob_from_moneyline(odds: dict | None) -> dict | None:
    """De-vigged win probability from the two moneylines.

    The book's two implied probabilities sum to >100% (that overround is the
    vig); normalising to 100 gives the honest split without a second request.
    """
    if not odds:
        return None
    money = odds.get("moneyline") or {}
    home = _american_to_implied(money.get("home"))
    away = _american_to_implied(money.get("away"))
    if home is None or away is None:
        return None
    total = home + away
    if total <= 0:
        return None
    return {
        "home": round(home / total * 100, 1),
        "away": round(away / total * 100, 1),
        "source": "moneyline",
    }


def _parse_situation(competition: dict, state: str) -> dict | None:
    """Live game state — bases/count for baseball, down & distance for football.

    Only meaningful while a game is in progress; ESPN leaves the key off
    entirely otherwise.
    """
    if state != "in":
        return None
    situation = competition.get("situation") or {}
    if not situation:
        return None
    out = {
        "last_play": ((situation.get("lastPlay") or {}).get("text")
                      if isinstance(situation.get("lastPlay"), dict)
                      else situation.get("lastPlay")),
        "balls": situation.get("balls"),
        "strikes": situation.get("strikes"),
        "outs": situation.get("outs"),
        "on_first": situation.get("onFirst"),
        "on_second": situation.get("onSecond"),
        "on_third": situation.get("onThird"),
        "down_distance": situation.get("downDistanceText"),
        "possession": situation.get("possession"),
        "red_zone": situation.get("isRedZone"),
    }
    return out if any(v is not None for v in out.values()) else None


def _parse_records(competitor: dict) -> dict:
    """All three record splits, keyed by ESPN's `type` (total/home/road)."""
    out: dict[str, str] = {}
    for record in competitor.get("records") or []:
        kind = record.get("type") or record.get("name")
        summary = record.get("summary")
        if not summary:
            continue
        if kind in ("total", "overall"):
            out["overall"] = summary
        elif kind == "home":
            out["home"] = summary
        elif kind in ("road", "away"):
            out["road"] = summary
    return out


def _parse_probable(competitor: dict) -> dict | None:
    """Probable starting pitcher (MLB) with a headline stat, for pre games."""
    probables = competitor.get("probables") or []
    if not probables:
        return None
    entry = probables[0] or {}
    athlete = entry.get("athlete") or {}
    name = athlete.get("shortName") or athlete.get("displayName")
    if not name:
        return None
    stats = {
        s.get("abbreviation") or s.get("name"): s.get("displayValue")
        for s in entry.get("statistics") or []
    }
    era = stats.get("ERA")
    wins, losses = stats.get("W"), stats.get("L")
    bits = []
    if wins is not None and losses is not None:
        bits.append(f"{wins}-{losses}")
    if era is not None:
        bits.append(f"{era} ERA")
    return {"name": name, "stat": " · ".join(bits) or None}


class SportsCollector(Collector):
    name = "sports"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.leagues: list[dict] = self.module_config.get(
            "leagues",
            [{"sport": "baseball", "league": "mlb"}],
        )
        self.followed = [t.lower() for t in self.module_config.get("followed_teams", [])]
        self.live_interval = float(self.module_config.get("poll_seconds_live", 30))
        self.idle_interval = float(self.module_config.get("poll_seconds_idle", 600))
        self.interval = self.live_interval
        # Schedule window: ESPN's undated scoreboard only returns "the current
        # slate" (for the NFL, the current *week* — which stays on last week's
        # finals for days). A dated request is the only way to see what's next,
        # but it costs ~2MB/league, so it rides its own slow lane.
        self.schedule_interval = float(
            self.module_config.get("poll_seconds_schedule", 1800)
        )
        self.days_back = int(self.module_config.get("days_back", 4))
        self.days_ahead = int(self.module_config.get("days_ahead", 7))
        self.max_recent = int(self.module_config.get("max_recent", 6))
        self.max_upcoming = int(self.module_config.get("max_upcoming", 6))
        self.league_status: dict[str, dict] = {}  # per-league fetch state for /api/health
        self.celebrations = self.module_config.get("celebrations", True)
        # Existing config DBs never gain new default keys, so every one of
        # these has to default here as well as in defaults.yaml.
        self.show_odds = self.module_config.get("show_odds", True)
        self.show_records = self.module_config.get("show_records", True)
        self.win_probability = self.module_config.get("win_probability", True)
        self.max_prob_games = int(self.module_config.get("max_prob_games", 4))
        self._bus: Bus | None = None
        self._side_scores: dict[str, tuple[int, int]] = {}  # game id -> (away, home)
        self._celebrated_at: dict[str, float] = {}
        self._window: list[dict] = []  # cached dated-window games
        # (league key, day) -> that day's games; settled days are reused
        self._window_days: dict[tuple[str, date], list[dict]] = {}
        self.window_status: dict | None = None  # last window refresh, for /api/health
        self._predictor: dict[str, tuple[float, dict | None]] = {}  # event id -> (stamp, prob)
        self._window_at: float = 0.0  # monotonic stamp of the last window fetch
        self._window_day: str = ""  # local date the window was built for

    async def start(self, bus: Bus) -> None:
        self._bus = bus  # kept for score-event broadcasts
        await super().start(bus)

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            results = await asyncio.gather(
                *(self._league(client, league) for league in self.leagues),
                return_exceptions=True,
            )
            # Slow lane, same client: refreshed on its own cadence, never
            # allowed to fail the poll.
            await self._refresh_window(client)
        games: list[dict] = []
        failures = 0
        now = datetime.now(timezone.utc).isoformat()
        status: dict[str, dict] = {}
        for league, result in zip(self.leagues, results):
            key = self._league_key(league)
            entry = {
                "sport": league.get("sport"),
                "league": league.get("league"),
                "checked_at": now,
            }
            if isinstance(result, BaseException):
                failures += 1
                log.debug("league %s failed: %s", league.get("league"), result)
                entry.update(ok=False, error=str(result), games=0)
            else:
                games.extend(result)
                entry.update(ok=True, error=None, games=len(result))
            status[key] = entry
        self.league_status = status
        if failures == len(self.leagues) and self.leagues:
            raise RuntimeError("all league scoreboards failed")
        window = self.window_status or {}
        self.degraded = (
            f"{failures}/{len(self.leagues)} league scoreboards failing"
            if failures
            else f"schedule window: {window['failed']}/{window['failed'] + window['fetched']} days failing"
            if window.get("failed")
            else None
        )
        # Celebrations diff the *fast* lane only — the window is up to 30 min
        # stale and would replay old scores as fresh ones.
        await self._maybe_celebrate(games)
        merged = self._merge_window(games)
        await self._refresh_probabilities(merged)
        return merged

    async def _league(
        self, client: httpx.AsyncClient, league: dict, params: dict | None = None
    ) -> list[dict]:
        response = await client.get(
            SCOREBOARD_URL.format(sport=league["sport"], league=league["league"]),
            params=params,
        )
        response.raise_for_status()
        data = response.json()
        return [
            game
            for event in data.get("events", [])
            if (game := self._parse_event(event, league)) is not None
        ]

    # ---- dated schedule window ---------------------------------------------

    async def _refresh_window(self, client: httpx.AsyncClient) -> None:
        """Re-fetch the days_back..days_ahead window when it goes stale or the
        local date rolls. Failures keep the previous window — a missing window
        costs upcoming games, not the whole module.

        One request per league per *day*: ESPN began rejecting
        `dates=YYYYMMDD-YYYYMMDD` ranges with a 400 (Sep 2026), while single
        days still work. A day that ended before yesterday is settled, so it is
        cached until it scrolls out of the window rather than re-fetched."""
        today = date.today()
        fresh = (
            self._window_at
            and self._window_day == today.isoformat()
            and time.monotonic() - self._window_at < self.schedule_interval
        )
        if fresh:
            return
        days = [
            today + timedelta(days=offset)
            for offset in range(-self.days_back, self.days_ahead + 1)
        ]
        settled_before = today - timedelta(days=1)
        wanted = {
            (self._league_key(league), day): league
            for league in self.leagues
            for day in days
        }
        # Anything no longer in the window (date rolled, league removed) goes.
        for key in list(self._window_days):
            if key not in wanted:
                del self._window_days[key]
        to_fetch = [
            key
            for key in wanted
            if not (key[1] < settled_before and key in self._window_days)
        ]
        gate = asyncio.Semaphore(WINDOW_CONCURRENCY)

        async def one(key: tuple[str, date]) -> list[dict]:
            async with gate:
                return await self._league(
                    client, wanted[key], {"dates": key[1].strftime("%Y%m%d")}
                )

        results = await asyncio.gather(*(one(k) for k in to_fetch), return_exceptions=True)
        failed = 0
        last_error = None
        for key, result in zip(to_fetch, results):
            if isinstance(result, BaseException):
                failed += 1
                last_error = f"{key[0]} {key[1]}: {type(result).__name__}: {result}"
                continue  # a cached copy of that day, if any, stays in use
            self._window_days[key] = result
        if to_fetch and failed == len(to_fetch):
            # Loud on purpose: this path failed silently for ten days when
            # ESPN changed its date syntax, while /health still said ok.
            log.warning("sports window: all %d day fetches failed (%s)", failed, last_error)
        elif failed:
            log.info("sports window: %d/%d day fetches failed (%s)", failed, len(to_fetch), last_error)
        self.window_status = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "days": len(days),
            "fetched": len(to_fetch) - failed,
            "failed": failed,
            "error": last_error,
        }
        if to_fetch and failed == len(to_fetch) and not self._window_days:
            return  # nothing at all yet; retry on the next poll, not in 30 min
        self._window = [
            game
            for key in wanted
            for game in self._window_days.get(key, [])
        ]
        self._window_at = time.monotonic()
        self._window_day = today.isoformat()
        log.debug("sports window: %d games over %d days", len(self._window), len(days))

    @staticmethod
    def _league_key(league: dict) -> str:
        return f"{league.get('sport')}/{league.get('league')}"

    # Fields the undated scoreboard sometimes omits while the dated window has
    # them (verified: the undated MLB scoreboard returns odds: null, the window
    # carries odds for near-term games). A whole-object overwrite would throw
    # them away every fast poll, so these carry over when the fresh game lacks
    # them.
    _CARRY_OVER = ("odds", "win_prob", "broadcast", "venue", "note")

    def _merge_window(self, live_games: list[dict]) -> list[dict]:
        """Window first, fast lane on top — same event id, fresher scores win."""
        merged: dict[str, dict] = {}
        for game in self._window:
            if game.get("id"):
                merged[str(game["id"])] = game
        for game in live_games:
            key = str(game["id"]) if game.get("id") else None
            if key is None:  # no id to merge on; keep it rather than drop it
                merged[f"_{len(merged)}"] = game
                continue
            previous = merged.get(key)
            if previous:
                for field in self._CARRY_OVER:
                    if game.get(field) is None and previous.get(field) is not None:
                        game[field] = previous[field]
                for side in ("home", "away"):
                    old_side, new_side = previous.get(side) or {}, game.get(side) or {}
                    if new_side.get("probable") is None and old_side.get("probable"):
                        new_side["probable"] = old_side["probable"]
                    if not new_side.get("records") and old_side.get("records"):
                        new_side["records"] = old_side["records"]
            merged[key] = game
        return list(merged.values())

    def _parse_event(self, event: dict, league: dict) -> dict | None:
        try:
            competition = (event.get("competitions") or [{}])[0]
            status_type = (event.get("status") or {}).get("type") or {}
            teams: dict[str, dict] = {}
            for competitor in competition.get("competitors", []):
                team = competitor.get("team") or {}
                records = competitor.get("records") or []
                teams[competitor.get("homeAway", "home")] = {
                    "abbrev": team.get("abbreviation", "?"),
                    "id": team.get("id"),  # ESPN team id — matches MLB play.team.id
                    "name": team.get("displayName", "?"),
                    "score": competitor.get("score"),
                    "logo": team.get("logo"),
                    "color": team.get("color"),
                    "record": records[0].get("summary") if records else None,
                    # home/road splits ride along free; `record` stays as-is so
                    # an older display bundle keeps working.
                    "records": _parse_records(competitor) if self.show_records else {},
                    "winner": competitor.get("winner"),
                    "probable": _parse_probable(competitor),
                    # per-period scores; ESPN populates these once the game is live
                    "linescores": [
                        v.get("value") for v in competitor.get("linescores") or []
                    ],
                }
            if "home" not in teams or "away" not in teams:
                return None
            state = status_type.get("state", "pre")  # pre | in | post
            venue = competition.get("venue") or {}
            notes = competition.get("notes") or []
            odds = _parse_odds(competition) if self.show_odds else None
            game = {
                "id": event.get("id"),
                "sport": league.get("sport"),
                "league": str(league["league"]).upper(),
                "state": state,
                "detail": status_type.get("shortDetail", ""),
                "start": event.get("date"),
                "home": teams["home"],
                "away": teams["away"],
                "followed": self._is_followed(teams),
                "odds": odds,
                # A book's line is itself a win forecast — free, and available
                # earlier than any of ESPN's model endpoints. The dedicated
                # probability lane overwrites this for live games.
                "win_prob": _prob_from_moneyline(odds) if self.win_probability else None,
                "situation": _parse_situation(competition, state),
                "broadcast": competition.get("broadcast")
                or next(
                    (
                        ", ".join(b.get("names") or [])
                        for b in competition.get("broadcasts") or []
                        if b.get("names")
                    ),
                    None,
                ),
                "venue": {
                    "name": venue.get("fullName"),
                    "city": (venue.get("address") or {}).get("city"),
                }
                if venue.get("fullName")
                else None,
                "note": (notes[0] or {}).get("headline") if notes else None,
            }
            # Which side is mine — the display leads with the followed game and
            # needs to know whether it's a home or away night.
            game["followed_side"] = self._followed_side(game)
            return game
        except Exception as exc:
            log.debug("unparseable event in %s: %s", league.get("league"), exc)
            return None

    def _is_followed(self, teams: dict) -> bool:
        names = " ".join(t.get("name", "") for t in teams.values()).lower()
        return any(team in names for team in self.followed)

    def _followed_side(self, game: dict) -> str | None:
        for side in ("away", "home"):
            name = game.get(side, {}).get("name", "").lower()
            if any(team in name for team in self.followed):
                return side
        return None

    # ---- win probability ---------------------------------------------------

    async def _refresh_probabilities(self, games: list[dict]) -> None:
        """Fill win_prob for followed games from ESPN's core API.

        Bounded on purpose: followed games only, capped at max_prob_games, and
        never allowed to fail the poll. Games that already got a probability
        from the moneyline only come here if they are *live* — a book's line is
        a pre-game forecast and goes stale the moment the game starts.
        """
        if not self.win_probability:
            return
        live = [g for g in games if g.get("followed") and g.get("state") == "in"]
        pre = [
            g
            for g in games
            if g.get("followed")
            and g.get("state") == "pre"
            and not g.get("win_prob")  # moneyline already answered this one
        ]
        targets = (live + pre)[: self.max_prob_games]
        if not targets:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            results = await asyncio.gather(
                *(
                    self._live_probability(client, g)
                    if g.get("state") == "in"
                    else self._predictor_probability(client, g)
                    for g in targets
                ),
                return_exceptions=True,
            )
        for game, result in zip(targets, results):
            if isinstance(result, BaseException):
                log.debug("win prob %s failed: %s", game.get("id"), result)
                continue
            if result:
                game["win_prob"] = result

    def _core_url(self, game: dict, resource: str) -> str:
        return CORE_URL.format(
            sport=game.get("sport"),
            league=str(game.get("league", "")).lower(),
            event=game.get("id"),
            resource=resource,
        )

    async def _live_probability(
        self, client: httpx.AsyncClient, game: dict
    ) -> dict | None:
        """Current in-game win probability: the last entry of the per-play feed.

        Paged rather than fetched whole — the full array is one entry per play,
        and only the newest one matters. Two ~1KB requests instead of the ~1MB
        the site summary endpoint would cost.
        """
        url = self._core_url(game, "probabilities")
        head = await client.get(url, params={"limit": 1})
        if head.status_code != 200:
            return None
        pages = (head.json() or {}).get("pageCount") or 0
        if not pages:
            return None
        last = await client.get(url, params={"limit": 1, "page": pages})
        if last.status_code != 200:
            return None
        items = (last.json() or {}).get("items") or []
        if not items:
            return None
        home = items[0].get("homeWinPercentage")
        away = items[0].get("awayWinPercentage")
        if home is None or away is None:
            return None
        return {
            "home": round(float(home) * 100, 1),
            "away": round(float(away) * 100, 1),
            "source": "live",
        }

    async def _predictor_probability(
        self, client: httpx.AsyncClient, game: dict
    ) -> dict | None:
        """ESPN's pre-game matchup projection.

        Only reached when there is no betting line to derive from. Not every
        league/season has it — NFL preseason 400s — so a miss is cached too,
        to stop us re-asking every poll for something that will never arrive.
        """
        key = str(game.get("id"))
        cached = self._predictor.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < self.schedule_interval:
            return cached[1]
        prob = None
        response = await client.get(self._core_url(game, "predictor"))
        if response.status_code == 200:
            data = response.json() or {}
            sides = {}
            for side, field in (("home", "homeTeam"), ("away", "awayTeam")):
                stats = {
                    stat.get("name"): stat.get("displayValue")
                    for stat in (data.get(field) or {}).get("statistics") or []
                }
                value = stats.get("gameProjection")
                if value is not None:
                    try:
                        sides[side] = round(float(value), 1)
                    except (TypeError, ValueError):
                        pass
            if len(sides) == 2:
                prob = {**sides, "source": "predictor"}
        self._predictor[key] = (now, prob)
        return prob

    # ---- score celebrations ------------------------------------------------

    async def _maybe_celebrate(self, games: list[dict]) -> None:
        """Diff per-side scores vs the previous poll; broadcast a sport_event
        when a followed team's score increases in a live game."""
        first_seed = not self._side_scores
        scored: list[tuple[dict, int]] = []
        new_scores: dict[str, tuple[int, int]] = {}
        now = time.monotonic()
        for g in games:
            gid = str(g.get("id"))

            def score_of(side: str) -> int:
                try:
                    return int(g[side].get("score") or 0)
                except (TypeError, ValueError):
                    return 0

            current = (score_of("away"), score_of("home"))
            new_scores[gid] = current
            if first_seed or not (g["followed"] and g["state"] == "in"):
                continue
            side = self._followed_side(g)
            if side is None:
                continue
            index = 0 if side == "away" else 1
            previous = self._side_scores.get(gid)
            if previous is None:
                continue
            delta = current[index] - previous[index]
            if delta <= 0:
                continue
            cooldown = SPORT_COOLDOWN_SECONDS.get(g.get("sport"), CELEBRATION_COOLDOWN_SECONDS)
            if now - self._celebrated_at.get(gid, 0.0) < cooldown:
                continue
            self._celebrated_at[gid] = now
            scored.append((g, delta))
        self._side_scores = new_scores
        if not self.celebrations or self._bus is None:
            return
        for game, delta in scored:
            try:
                event = await self._build_live_event(game, delta)
                await self._bus.broadcast({"type": "sport_event", "event": event})
                log.info("score celebration: %s %s", event["team"]["abbrev"], event["label"])
            except Exception as exc:
                log.warning("could not build score event: %s", exc)

    async def _build_live_event(self, game: dict, delta: int) -> dict:
        summary: dict = {}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    SUMMARY_URL.format(
                        sport=game.get("sport"), league=str(game.get("league", "")).lower()
                    ),
                    params={"event": game.get("id")},
                )
                response.raise_for_status()
                summary = response.json()
        except Exception as exc:  # best-effort: celebrate without play detail
            log.debug("summary fetch failed for %s: %s", game.get("id"), exc)

        side = self._followed_side(game) or "home"
        team = game[side]
        opponent = game["home" if side == "away" else "away"]
        play = (
            latest_scoring_play(summary, team.get("abbrev"), team_id=team.get("id"))
            if summary
            else None
        )
        text = (play or {}).get("text", "")
        label = (
            _label_from_type(((play or {}).get("type") or {}).get("text") or "")
            or _delta_label(game.get("sport"), delta, text)
        )
        scorer = None
        if play and summary:
            scorer = _scorer_from_participants(summary, play) or _scorer_from_text(
                summary, text
            )
        return {
            "sport": game.get("sport"),
            "league": game.get("league"),
            "label": label,
            "text": text,
            "team": {k: team.get(k) for k in ("abbrev", "name", "color", "logo")},
            "opponent": {k: opponent.get(k) for k in ("abbrev", "name", "color", "logo")},
            "away_score": (play or {}).get("awayScore", game["away"].get("score")),
            "home_score": (play or {}).get("homeScore", game["home"].get("score")),
            "team_is_home": side == "home",
            "scorer": scorer,
        }

    def status(self) -> dict:
        return super().status() | {
            "leagues": list(self.league_status.values()),
            "window": self.window_status,
        }

    def _pick(self, games: list[dict], limit: int) -> list[dict]:
        """Reserve a seat for each followed team's nearest game, then fill the
        rest in the order given. Without the reservation a busy slate buries the
        one game the owner actually cares about."""
        chosen: list[dict] = []
        seen: set[int] = set()
        claimed: set[str] = set()
        for game in games:
            side = game.get("followed_side")
            if not side:
                continue
            name = (game.get(side) or {}).get("name", "")
            if name in claimed:
                continue  # that team already has its nearest game in
            claimed.add(name)
            seen.add(id(game))
            chosen.append(game)
        for game in games:
            if len(chosen) >= limit:
                break
            if id(game) not in seen:
                chosen.append(game)
        return chosen[:limit]

    def shape(self, games: list[dict]) -> ModulePayload:
        self.interval = (
            self.live_interval
            if any(g["state"] == "in" for g in games)
            else self.idle_interval
        )
        horizon = (
            datetime.now(timezone.utc) + timedelta(days=self.days_ahead)
        ).isoformat()

        # Newest result first; the display reverses the past group so the
        # freshest final ends up against the now line.
        recent = sorted(
            (g for g in games if g["state"] == "post"),
            key=lambda g: g["start"] or "",
            reverse=True,
        )
        live = sorted(
            (g for g in games if g["state"] == "in"), key=lambda g: g["start"] or ""
        )
        upcoming = sorted(
            (g for g in games if g["state"] == "pre" and (g["start"] or "") <= horizon),
            key=lambda g: g["start"] or "",
        )

        recent = self._pick(recent, self.max_recent)
        recent.sort(key=lambda g: g["start"] or "", reverse=True)
        upcoming = self._pick(upcoming, self.max_upcoming)
        upcoming.sort(key=lambda g: g["start"] or "")

        for bucket, items in (
            ("recent", recent),
            ("live", live),
            ("next", upcoming),
        ):
            for game in items:
                game["bucket"] = bucket

        next_up = next((g["id"] for g in upcoming if g["followed"]), None)

        tape = []
        for g in [*live, *recent, *upcoming]:
            if g["state"] == "pre":
                text = f"{g['away']['abbrev']} at {g['home']['abbrev']} · {self._tape_when(g['start'])}"
                line = (g.get("odds") or {}).get("details")
                if line and self.show_odds:
                    text = f"{text} · {line}"
            else:
                live_now = g["state"] == "in"
                suffix = g["detail"] if live_now else "F"
                text = (
                    f"{g['away']['abbrev']} {g['away']['score']} – "
                    f"{g['home']['abbrev']} {g['home']['score']} ({suffix})"
                )
            tape.append(
                TapeItem(
                    text=text,
                    accent="alert" if g["state"] == "in" else "neutral",
                    priority=1 if g["followed"] else 0,
                    icon=g.get("sport"),
                )
            )
        return ModulePayload(
            module=self.name,
            stage={"games": [*recent, *live, *upcoming], "next_up": next_up},
            tape=tape[:12],
        )

    @staticmethod
    def _tape_when(start: str | None) -> str:
        """Absolute local day + time — tape strings are static until the next
        republish, so a relative countdown would rot on screen."""
        if not start:
            return "TBD"
        try:
            when = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return "TBD"
        return when.strftime("%a %-I:%M %p")
