"""Sports collector — ESPN public scoreboard endpoints (unofficial).

All parsing is isolated in shape()/_parse_event so an upstream format change
degrades to a stale module, never a crash. Poll rate tightens automatically
while any followed-league game is live.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
STATE_SORT = {"in": 0, "pre": 1, "post": 2}


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
