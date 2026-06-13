"""Fantasy football collector — ESPN's (unofficial) v3 fantasy API.

One league request returns everything: my matchup, full standings, the week's
scoreboard, and per-team weekly results. Win probability is computed locally
(ESPN exposes no fantasy WP) from each side's live projected final.

Private leagues need the browser cookies ESPN_S2 + ESPN_SWID in the env; public
leagues read with no auth. All parsing is isolated in shape()/_side so an
upstream format change degrades to a stale module, never a crash. Poll rate
tightens automatically while my matchup is live.
"""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone

import httpx

from ..state import Bus, ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

# Read host (the bare fantasy.espn.com 302-redirects here).
LEAGUE_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leagues/{league_id}"
)
VIEWS = ("mTeam", "mRoster", "mMatchupScore", "mScoreboard", "mSettings", "mStandings")
HEADERS = {"User-Agent": "edge-ticker/1.0 (fantasy football collector)", "Accept": "application/json"}

CELEBRATION_COOLDOWN_SECONDS = 120.0
HEADSHOT_URL = "https://a.espncdn.com/i/headshots/nfl/players/full/{pid}.png"

# Custom (user-uploaded) team logos are served from this auth-only host — the
# cookieless kiosk browser gets a 401, so rewrite them to a backend proxy that
# refetches with the server's cookies. Built-in logos (g.espncdn.com) are public
# and pass through unchanged. The proxy lives in backend/api/fantasy.py.
LOGO_IMAGE_PREFIX = "https://mystique-api.fantasy.espn.com/apis/v1/domains/lm/images/"

# Win-probability normal model: remaining projected points drive the variance,
# so the result sharpens toward 100/0 as a slate finishes. SIGMA_K is tuned so a
# full untouched slate (~110 projected each) yields sigma ≈ 24.
SIGMA_K = 1.6

# lineupSlotId 20 = bench, 21 = IR; everything else is a starting slot.
BENCH_SLOTS = {20, 21}
SLOT_LABELS = {
    0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K",
    23: "FLEX", 7: "OP", 1: "TQB", 3: "RB/WR", 5: "WR/TE", 24: "ER",
}
# player.defaultPositionId → position label
POS_LABELS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
# injuryStatus → short badge ("" / None means healthy, no badge)
INJURY_BADGE = {
    "QUESTIONABLE": "Q", "DOUBTFUL": "D", "OUT": "OUT", "PROBABLE": "P",
    "SUSPENSION": "SUS", "SUSPENDED": "SUS", "INJURY_RESERVE": "IR", "IR": "IR",
    "DAY_TO_DAY": "DTD",
}
# badges that mean a starter probably needs swapping
UNAVAILABLE = {"OUT", "D", "SUS", "IR"}

# Per-season NFL schedule (abbrev, bye week, weekly opponent+kickoff). Public,
# auth-free, changes rarely → cached. Shared by the collector and the API.
_PRO_TTL_SECONDS = 6 * 3600.0
_pro_cache: dict[int, tuple[float, dict]] = {}


async def pro_teams(season: int) -> dict:
    """{proTeamId: {abbrev, byeWeek, games: {week: {oppId, oppAbbrev, home, date}}}}."""
    now = time.monotonic()
    cached = _pro_cache.get(season)
    if cached and now - cached[0] < _PRO_TTL_SECONDS:
        return cached[1]
    url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url, params={"view": "proTeamSchedules_wl"})
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # best-effort: enrichment degrades to no opp/bye
        log.warning("pro_teams fetch failed: %s", exc)
        return cached[1] if cached else {}
    proteams = (data.get("settings") or {}).get("proTeams") or []
    abbrev_by_id = {t.get("id"): (t.get("abbrev") or "").upper() for t in proteams}
    out: dict[int, dict] = {}
    for t in proteams:
        tid = t.get("id")
        games: dict[int, dict] = {}
        for wk, glist in (t.get("proGamesByScoringPeriod") or {}).items():
            g = (glist or [{}])[0]
            is_home = g.get("homeProTeamId") == tid
            opp_id = g.get("awayProTeamId") if is_home else g.get("homeProTeamId")
            games[int(wk)] = {
                "oppId": opp_id,
                "oppAbbrev": abbrev_by_id.get(opp_id, ""),
                "home": is_home,
                "date": g.get("date"),
            }
        out[tid] = {"abbrev": abbrev_by_id.get(tid), "byeWeek": t.get("byeWeek"), "games": games}
    _pro_cache[season] = (now, out)
    return out


def _injury_badge(player: dict) -> str | None:
    raw = player.get("injuryStatus")
    if not raw or raw in ("ACTIVE", "NORMAL"):
        return None
    return INJURY_BADGE.get(raw, raw[:3].upper())


def player_meta(player: dict, pro: dict, week: int) -> dict:
    """Position, NFL team, injury, bye, this-week opponent + kickoff for a player."""
    pt = pro.get(player.get("proTeamId")) or {}
    bye = pt.get("byeWeek") == week and pt.get("byeWeek") is not None
    game = (pt.get("games") or {}).get(week)
    opp = None
    kickoff = None
    if bye:
        opp = "BYE"
    elif game:
        opp = ("@ " if not game["home"] else "vs ") + (game["oppAbbrev"] or "?")
        kickoff = game.get("date")
    return {
        "pos": POS_LABELS.get(player.get("defaultPositionId"), ""),
        "proTeam": pt.get("abbrev"),
        "injury": _injury_badge(player),
        "injured": bool(player.get("injured")),
        "bye": bye,
        "opp": opp,
        "kickoff": kickoff,
    }


def win_probability(proj_a: float, proj_b: float, rem_a: float, rem_b: float) -> float:
    """P(team A finishes ahead of team B), 0–100, from projected finals."""
    var = max(0.0, rem_a) + max(0.0, rem_b)
    sigma = math.sqrt(var) * SIGMA_K
    if sigma < 1e-6:  # nothing left to play — decided by (projected==final) points
        if proj_a > proj_b:
            return 100.0
        if proj_a < proj_b:
            return 0.0
        return 50.0
    z = (proj_a - proj_b) / (sigma * math.sqrt(2.0))
    return round(50.0 * (1.0 + math.erf(z)), 1)


class FantasyCollector(Collector):
    name = "fantasy"
    enabled_by_default = True

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.league_id = str(self.module_config.get("league_id", "") or "").strip()
        self.team_id = self.module_config.get("team_id")
        self.team_name = str(self.module_config.get("team_name", "") or "").strip().lower()
        self.season = int(
            self.module_config.get("season") or datetime.now(timezone.utc).year
        )
        self.live_interval = float(self.module_config.get("poll_seconds_live", 30))
        self.idle_interval = float(self.module_config.get("poll_seconds_idle", 1800))
        self.interval = self.idle_interval
        self.celebrations = self.module_config.get("celebrations", True)
        self.espn_s2 = os.environ.get("ESPN_S2", "").strip()
        self.espn_swid = os.environ.get("ESPN_SWID", "").strip()
        self._client: httpx.AsyncClient | None = None
        self._bus: Bus | None = None
        self._pro: dict = {}  # per-season NFL schedule (abbrev/bye/opponent)
        self._side_points: dict[str, float] = {}  # teamId -> last live points (for celebrations)
        self._celebrated_at: dict[str, float] = {}

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        await super().start(bus)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            cookies = {}
            if self.espn_s2 and self.espn_swid:
                cookies = {"espn_s2": self.espn_s2, "SWID": self.espn_swid}
            self._client = httpx.AsyncClient(
                timeout=15, headers=HEADERS, cookies=cookies, follow_redirects=True
            )
        return self._client

    async def fetch(self) -> dict:
        if not self.league_id:
            raise RuntimeError("fantasy: league_id is not configured")
        client = self._get_client()
        params = [("view", v) for v in VIEWS]
        response = await client.get(
            LEAGUE_URL.format(season=self.season, league_id=self.league_id), params=params
        )
        if response.status_code == 401:
            raise RuntimeError("fantasy: 401 — private league needs ESPN_S2/ESPN_SWID cookies")
        response.raise_for_status()
        self._pro = await pro_teams(self.season)
        return response.json()

    # ---- shaping -----------------------------------------------------------

    def _resolve_my_team_id(self, teams: list[dict]) -> int | None:
        if self.team_id is not None:
            return int(self.team_id)
        if self.team_name:
            for t in teams:
                if self.team_name in _team_name(t).lower() or self.team_name == (
                    t.get("abbrev") or ""
                ).lower():
                    return t.get("id")
        return teams[0].get("id") if teams else None

    def _side(
        self, raw_side: dict, teams_by_id: dict, members_by_id: dict, decided: bool, week: int
    ) -> dict:
        tid = raw_side.get("teamId")
        team = teams_by_id.get(tid, {})
        cur = raw_side.get("totalPointsLive")
        if cur is None:
            cur = raw_side.get("totalPoints") or 0.0
        proj = raw_side.get("totalProjectedPointsLive")
        starters = _starters(raw_side, self._pro, week)
        return {
            "teamId": tid,
            "name": _team_name(team),
            "abbrev": team.get("abbrev") or "?",
            "logo": _logo(team.get("logo")),
            "owner": _owner_name(team, members_by_id),
            "record": _record_summary(team),
            "color": None,
            "points": round(float(cur), 1),
            "projected": round(float(proj), 1) if proj is not None else None,
            "starters": starters,
        }

    def shape(self, raw: dict) -> ModulePayload:
        teams = raw.get("teams") or []
        teams_by_id = {t.get("id"): t for t in teams}
        members_by_id = {m.get("id"): m for m in (raw.get("members") or [])}
        settings = raw.get("settings") or {}
        status = raw.get("status") or {}
        week = int(status.get("currentMatchupPeriod") or raw.get("scoringPeriodId") or 1)
        season_active = bool(status.get("isActive"))

        schedule = raw.get("schedule") or []
        my_id = self._resolve_my_team_id(teams)

        # This week's matchups (the scoreboard) + my matchup.
        scoreboard: list[dict] = []
        my_matchup: dict | None = None
        for m in schedule:
            if int(m.get("matchupPeriodId") or 0) != week:
                continue
            home_raw, away_raw = m.get("home") or {}, m.get("away") or {}
            if not home_raw and not away_raw:
                continue
            winner = m.get("winner") or "UNDECIDED"
            decided = winner not in ("UNDECIDED", None)
            home = self._side(home_raw, teams_by_id, members_by_id, decided, week)
            away = (
                self._side(away_raw, teams_by_id, members_by_id, decided, week)
                if away_raw else None
            )
            entry = _matchup_entry(home, away, winner, decided, season_active, my_id)
            scoreboard.append(entry)
            if my_id in (home.get("teamId"), away.get("teamId") if away else None):
                my_matchup = entry
        if my_matchup and my_matchup.get("mineSide") in ("home", "away"):
            my_matchup["attention"] = _attention_count(my_matchup[my_matchup["mineSide"]])

        standings = _standings(teams, teams_by_id, members_by_id, my_id)
        my_team = next((s for s in standings if s.get("teamId") == my_id), None)
        trend = _trend(schedule, week, my_id, teams_by_id)

        stage = {
            "meta": {
                "league": settings.get("name") or "Fantasy",
                "leagueId": self.league_id,
                "season": self.season,
                "week": week,
                "seasonActive": season_active,
            },
            "myTeam": my_team,
            "matchup": my_matchup,
            "standings": standings,
            "scoreboard": scoreboard,
            "trend": trend,
        }

        self._set_interval(my_matchup)
        self._maybe_celebrate(my_matchup, away_lookup=teams_by_id)
        return ModulePayload(module=self.name, stage=stage, tape=_tape(stage))

    def _set_interval(self, my_matchup: dict | None) -> None:
        live = bool(my_matchup and my_matchup.get("state") == "in")
        self.interval = self.live_interval if live else self.idle_interval

    # ---- celebrations ------------------------------------------------------

    def _maybe_celebrate(self, my_matchup: dict | None, away_lookup: dict) -> None:
        if not my_matchup or my_matchup.get("state") != "in":
            return
        mine = my_matchup.get("mineSide")
        if mine not in ("home", "away"):
            return
        side = my_matchup[mine]
        opp = my_matchup["away" if mine == "home" else "home"]
        tid = str(side.get("teamId"))
        prev = self._side_points.get(tid)
        cur = float(side.get("points") or 0.0)
        first_seed = tid not in self._side_points
        self._side_points[tid] = cur
        if first_seed or prev is None or not self.celebrations or self._bus is None:
            return
        delta = cur - prev
        if delta < 0.1:
            return
        now = time.monotonic()
        if now - self._celebrated_at.get(tid, 0.0) < CELEBRATION_COOLDOWN_SECONDS:
            return
        self._celebrated_at[tid] = now
        scorer = _top_scorer(side)
        event = {
            "sport": "football",
            "league": "FANTASY",
            "label": "YOUR TEAM SCORES",
            "text": f"+{delta:.1f} pts" + (f" · {scorer['name']}" if scorer else ""),
            "team": {"abbrev": side.get("abbrev"), "name": side.get("name"),
                     "color": side.get("color"), "logo": side.get("logo")},
            "opponent": {"abbrev": opp.get("abbrev"), "name": opp.get("name"),
                         "color": opp.get("color"), "logo": opp.get("logo")},
            "away_score": (side if mine == "away" else opp).get("points"),
            "home_score": (side if mine == "home" else opp).get("points"),
            "team_is_home": mine == "home",
            "scorer": scorer,
        }
        import asyncio

        asyncio.create_task(self._bus.broadcast({"type": "fantasy_event", "event": event}))
        log.info("fantasy celebration: %s +%.1f", side.get("abbrev"), delta)


# ---- module-level helpers (pure, testable) --------------------------------


def _logo(url: str | None) -> str | None:
    """Rewrite auth-only custom logos to the backend proxy; pass public ones."""
    if url and url.startswith(LOGO_IMAGE_PREFIX):
        image_id = url[len(LOGO_IMAGE_PREFIX):].split("?", 1)[0]
        return f"/api/fantasy/logo?id={image_id}"
    return url


def _team_name(team: dict) -> str:
    name = team.get("name")
    if name:
        return name
    combined = f"{team.get('location') or ''} {team.get('nickname') or ''}".strip()
    return combined or team.get("abbrev") or "Team"


def _owner_name(team: dict, members_by_id: dict) -> str | None:
    owners = team.get("owners") or []
    if not owners:
        return None
    m = members_by_id.get(owners[0]) or {}
    return m.get("displayName") or (m.get("firstName") or "").strip() or None


def _record_summary(team: dict) -> str | None:
    o = (team.get("record") or {}).get("overall") or {}
    w, l, t = o.get("wins"), o.get("losses"), o.get("ties")
    if w is None and l is None:
        return None
    base = f"{w or 0}-{l or 0}"
    return f"{base}-{t}" if t else base


def _starters(raw_side: dict, pro: dict, week: int) -> list[dict]:
    roster = (
        raw_side.get("rosterForCurrentMatchupPeriod")
        or raw_side.get("rosterForMatchupPeriod")
        or {}
    )
    out = []
    for e in roster.get("entries") or []:
        slot = e.get("lineupSlotId")
        if slot in BENCH_SLOTS:
            continue
        player = (e.get("playerPoolEntry") or {}).get("player") or {}
        actual, proj = _player_points(player)
        out.append({
            "name": player.get("fullName") or "—",
            "slot": SLOT_LABELS.get(slot, ""),
            "playerId": (e.get("playerPoolEntry") or {}).get("id") or player.get("id"),
            "points": round(actual, 1) if actual is not None else None,
            "projected": round(proj, 1) if proj is not None else None,
            "yetToPlay": actual in (None, 0) and proj not in (None, 0),
            **player_meta(player, pro, week),
        })
    return out


def _attention_count(side: dict) -> int:
    """Starters that look like they need a swap: out/doubtful/IR/suspended/bye."""
    return sum(
        1
        for s in side.get("starters") or []
        if s.get("bye") or s.get("injured") or (s.get("injury") in UNAVAILABLE)
    )


def _player_points(player: dict) -> tuple[float | None, float | None]:
    """(actual, projected) for the player's scored period. statSourceId 0 =
    actual, 1 = projected; prefer the single-period split (statSplitTypeId 1)."""
    actual = proj = None
    for st in player.get("stats") or []:
        if st.get("statSplitTypeId") not in (None, 1):
            continue
        total = st.get("appliedTotal")
        if total is None:
            continue
        if st.get("statSourceId") == 0:
            actual = total
        elif st.get("statSourceId") == 1:
            proj = total
    return actual, proj


def _top_scorer(side: dict) -> dict | None:
    best = None
    for s in side.get("starters") or []:
        pts = s.get("points")
        if pts is None:
            continue
        if best is None or pts > (best.get("points") or 0):
            best = s
    if not best or not best.get("playerId"):
        return None
    return {"name": best.get("name"), "headshot": HEADSHOT_URL.format(pid=best["playerId"])}


def _matchup_entry(
    home: dict, away: dict | None, winner: str, decided: bool, season_active: bool, my_id
) -> dict:
    if away is None:  # bye week
        state = "post" if decided else "pre"
        return {"home": home, "away": None, "state": state, "winner": winner,
                "winProbability": None, "mineSide": "home" if home["teamId"] == my_id else None}
    any_points = (home["points"] or 0) > 0 or (away["points"] or 0) > 0
    if decided:
        state = "post"
    elif any_points and season_active:
        state = "in"
    else:
        state = "pre"

    def proj_rem(side):
        cur = side["points"] or 0.0
        proj = side["projected"]
        if state == "post" or proj is None:
            return cur, 0.0
        return proj, max(0.0, proj - cur)

    hp, hr = proj_rem(home)
    ap, ar = proj_rem(away)
    home_wp = win_probability(hp, ap, hr, ar)
    mine = "home" if home["teamId"] == my_id else "away" if away["teamId"] == my_id else None
    return {
        "home": home,
        "away": away,
        "state": state,
        "winner": winner,
        "winProbability": {"home_pct": home_wp, "away_pct": round(100.0 - home_wp, 1)},
        "mineSide": mine,
    }


def _standings(teams: list[dict], teams_by_id: dict, members_by_id: dict, my_id) -> list[dict]:
    rows = []
    for t in teams:
        o = (t.get("record") or {}).get("overall") or {}
        rows.append({
            "teamId": t.get("id"),
            "name": _team_name(t),
            "abbrev": t.get("abbrev") or "?",
            "logo": _logo(t.get("logo")),
            "owner": _owner_name(t, members_by_id),
            "wins": o.get("wins") or 0,
            "losses": o.get("losses") or 0,
            "ties": o.get("ties") or 0,
            "pointsFor": round(o.get("pointsFor") or 0.0, 1),
            "pointsAgainst": round(o.get("pointsAgainst") or 0.0, 1),
            "seed": t.get("playoffSeed") or 0,
            "mine": t.get("id") == my_id,
        })
    # Prefer ESPN's computed seed; fall back to wins then points-for.
    if all(r["seed"] for r in rows):
        rows.sort(key=lambda r: r["seed"])
    else:
        rows.sort(key=lambda r: (-r["wins"], -r["pointsFor"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = r["seed"] or i
    return rows


def _trend(schedule: list[dict], week: int, my_id, teams_by_id: dict) -> list[dict]:
    """My completed results so far this season: week, opponent, points, W/L."""
    out = []
    for m in schedule:
        mp = int(m.get("matchupPeriodId") or 0)
        if mp >= week:
            continue
        home, away = m.get("home") or {}, m.get("away") or {}
        if my_id not in (home.get("teamId"), away.get("teamId")):
            continue
        mine, theirs = (home, away) if home.get("teamId") == my_id else (away, home)
        my_pts = mine.get("totalPoints") or 0.0
        opp_pts = theirs.get("totalPoints") or 0.0
        opp = teams_by_id.get(theirs.get("teamId"), {})
        result = "W" if my_pts > opp_pts else "L" if my_pts < opp_pts else "T"
        out.append({
            "week": mp,
            "points": round(my_pts, 1),
            "opponent": opp.get("abbrev") or "?",
            "opponentPoints": round(opp_pts, 1),
            "result": result,
        })
    out.sort(key=lambda r: r["week"])
    return out


def _tape(stage: dict) -> list[TapeItem]:
    items: list[TapeItem] = []
    m = stage.get("matchup")
    if m and m.get("away"):
        mine = m.get("mineSide")
        me = m[mine] if mine in ("home", "away") else m["home"]
        opp = m["away" if mine == "home" else "home"] if mine else m["away"]
        wp = m.get("winProbability") or {}
        my_wp = wp.get(f"{mine}_pct") if mine else wp.get("home_pct")
        suffix = {"in": "LIVE", "post": "F", "pre": ""}.get(m.get("state"), "")
        wp_txt = f" · {my_wp:.0f}% win" if my_wp is not None and m.get("state") != "post" else ""
        accent = "neutral"
        if me["points"] is not None and opp["points"] is not None:
            accent = "up" if me["points"] >= opp["points"] else "down"
        text = (
            f"FFL: {me['abbrev']} {me['points']:.1f} vs "
            f"{opp['abbrev']} {opp['points']:.1f}{wp_txt}"
        )
        if suffix:
            text += f" ({suffix})"
        items.append(TapeItem(text=text, accent=accent, priority=1, icon="football"))
    my_team = stage.get("myTeam")
    if my_team:
        rec = f"{my_team['wins']}-{my_team['losses']}" + (
            f"-{my_team['ties']}" if my_team["ties"] else ""
        )
        items.append(
            TapeItem(text=f"FFL: {my_team['abbrev']} {rec}, #{my_team['rank']} · "
                     f"{my_team['pointsFor']:.0f} PF", accent="neutral")
        )
    return items
