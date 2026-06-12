"""Sports collector — ESPN public scoreboard endpoints (unofficial).

All parsing is isolated in shape()/_parse_event so an upstream format change
degrades to a stale module, never a crash. Poll rate tightens automatically
while any followed-league game is live.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from ..state import Bus, ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary"
SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team}/schedule"
STATE_SORT = {"in": 0, "pre": 1, "post": 2}

# Followed-team score celebrations: don't fire more than once per game per
# cooldown (basketball would otherwise celebrate every poll).
CELEBRATION_COOLDOWN_SECONDS = 180.0

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
    summary: dict, team_abbrev: str | None, prefer_touchdown: bool = False
) -> dict | None:
    plays = summary.get("scoringPlays") or [
        p for p in summary.get("plays", []) if p.get("scoringPlay")
    ]
    if team_abbrev:
        team_plays = [
            p for p in plays if (p.get("team") or {}).get("abbreviation") == team_abbrev
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
        self.league_status: dict[str, dict] = {}  # per-league fetch state for /api/health
        self.celebrations = self.module_config.get("celebrations", True)
        self._bus: Bus | None = None
        self._side_scores: dict[str, tuple[int, int]] = {}  # game id -> (away, home)
        self._celebrated_at: dict[str, float] = {}

    async def start(self, bus: Bus) -> None:
        self._bus = bus  # kept for score-event broadcasts
        await super().start(bus)

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            results = await asyncio.gather(
                *(self._league(client, league) for league in self.leagues),
                return_exceptions=True,
            )
        games: list[dict] = []
        failures = 0
        now = datetime.now(timezone.utc).isoformat()
        status: dict[str, dict] = {}
        for league, result in zip(self.leagues, results):
            key = f"{league.get('sport')}/{league.get('league')}"
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
        await self._maybe_celebrate(games)
        return games

    async def _league(self, client: httpx.AsyncClient, league: dict) -> list[dict]:
        response = await client.get(
            SCOREBOARD_URL.format(sport=league["sport"], league=league["league"])
        )
        response.raise_for_status()
        data = response.json()
        return [
            game
            for event in data.get("events", [])
            if (game := self._parse_event(event, league)) is not None
        ]

    def _parse_event(self, event: dict, league: dict) -> dict | None:
        try:
            competition = (event.get("competitions") or [{}])[0]
            status_type = (event.get("status") or {}).get("type") or {}
            teams: dict[str, dict] = {}
            for competitor in competition.get("competitors", []):
                team = competitor.get("team") or {}
                teams[competitor.get("homeAway", "home")] = {
                    "abbrev": team.get("abbreviation", "?"),
                    "name": team.get("displayName", "?"),
                    "score": competitor.get("score"),
                    "logo": team.get("logo"),
                    "color": team.get("color"),
                }
            if "home" not in teams or "away" not in teams:
                return None
            return {
                "id": event.get("id"),
                "sport": league.get("sport"),
                "league": str(league["league"]).upper(),
                "state": status_type.get("state", "pre"),  # pre | in | post
                "detail": status_type.get("shortDetail", ""),
                "start": event.get("date"),
                "home": teams["home"],
                "away": teams["away"],
                "followed": self._is_followed(teams),
            }
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
            if now - self._celebrated_at.get(gid, 0.0) < CELEBRATION_COOLDOWN_SECONDS:
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
        play = latest_scoring_play(summary, team.get("abbrev")) if summary else None
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
        return super().status() | {"leagues": list(self.league_status.values())}

    def shape(self, games: list[dict]) -> ModulePayload:
        games.sort(
            key=lambda g: (not g["followed"], STATE_SORT.get(g["state"], 3), g["start"] or "")
        )
        self.interval = (
            self.live_interval
            if any(g["state"] == "in" for g in games)
            else self.idle_interval
        )
        tape = []
        for g in games:
            if g["state"] == "pre":
                continue
            live = g["state"] == "in"
            suffix = g["detail"] if live else "F"
            tape.append(
                TapeItem(
                    text=f"{g['away']['abbrev']} {g['away']['score']} – "
                    f"{g['home']['abbrev']} {g['home']['score']} ({suffix})",
                    accent="alert" if live else "neutral",
                    priority=1 if g["followed"] else 0,
                    icon=g.get("sport"),
                )
            )
        return ModulePayload(
            module=self.name, stage={"games": games[:12]}, tape=tape[:12]
        )
