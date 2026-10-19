"""WARDOGS player stats — wdstats.co.uk's JSON API (keyless).

wdstats is a fan-run tracker: a player signs in there once with Steam and the
site re-syncs their profile roughly hourly. Two calls per poll:

  /api/players/{id}  Wardog level (the sum of the class levels), career XP,
                     class levels/XP with leaderboard ranks, cash, gold bars,
                     unlocks (each with a global owner count, hence rarity)
                     and an XP history.
  /api/gold          the in-game gold bar price (global, one price a day) with
                     1/7/30-day change and a daily series. Best effort: a
                     failure keeps the last price and marks the module degraded.

A hobby site with nothing newer than its hourly sync: the poll defaults to
15 min and is floored at 300 s, and failures back off slowly. /combat is not
fetched — combat stats only come from tracked community servers.

Seasons: the player payload carries the latest season the player has data for.
At a rollover it stays on the previous season's final figures until the
player's first sync in the new one, so the page labels which season it shows.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

BASE_URL = "https://wdstats.co.uk"
HEADERS = {"User-Agent": "edge-ticker (personal kiosk display)", "Accept": "application/json"}
DEFAULT_PLAYER = "76561197988260454"
# A Steam64 id, or the 12-hex slug wdstats gives players who hide theirs.
PLAYER_ID = re.compile(r"7656\d{13}|[0-9a-f]{12}")
MIN_POLL_SECONDS = 300.0
LEVEL_UP_TTL = timedelta(hours=1)
RAREST = 5
SPARK_DAYS = 30
AVATAR_HOSTS = ("steamstatic.com", "steamcdn-a.akamaihd.net")
ROLE_ICONS = {
    "Infantry": "wd-assault", "Medic": "wd-medic", "Recon": "wd-recon",
    "Support": "wd-support", "Driver": "wd-driver", "Pilot": "wd-pilot",
}

# Module-level on purpose (the opnsense precedent): a config save restarts the
# collector, and instance state would drop the gold price on the next failure
# and forget the levels a level-up is measured against.
_gold: dict = {"payload": None}
_levels: dict[str, dict] = {}  # player id -> {"season", "levels": {key: lvl}, "ups": [...]}


def _now() -> datetime:
    return datetime.now(timezone.utc)  # a function so tests can pin the clock


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def compact(n: int | float | None) -> str:
    """1697761 -> 1.70M, 176953 -> 177K, 48213 -> 48.2K (wdstats' own style)."""
    if n is None:
        return "—"
    size = abs(n)
    if size >= 1e6:
        return f"{n / 1e6:.2f}M"
    if size >= 1e5:
        return f"{n / 1e3:.0f}K"
    if size >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{n:.0f}"


def _rank(entry: object) -> dict | None:
    if not isinstance(entry, dict) or _int(entry.get("rank")) is None:
        return None
    return {"rank": _int(entry.get("rank")), "total": _int(entry.get("total"))}


def _avatar(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in AVATAR_HOSTS):
        return None
    return url


def _gain(history: object, since: datetime) -> int | None:
    """Career XP gained since `since`. The baseline is the last point at or
    before it, else the earliest point; None when there is no baseline (the
    player was first tracked inside the window). History only records changes,
    so a latest point older than `since` means nothing was gained."""
    points = sorted(
        (t, xp)
        for p in history or []
        if isinstance(p, dict)
        and (t := _parse(p.get("t"))) is not None
        and (xp := _int(p.get("xp"))) is not None
    )
    if not points:
        return None
    latest_t, latest_xp = points[-1]
    if latest_t <= since:
        return 0
    before = [p for p in points if p[0] <= since]
    if not before and len(points) < 2:
        return None
    base_xp = (before[-1] if before else points[0])[1]
    return max(0, latest_xp - base_xp)


def _gold_stage(raw: object) -> dict | None:
    if not isinstance(raw, dict) or _int(raw.get("price")) is None:
        return None
    changes = {
        _int(c.get("days")): c for c in raw.get("changes") or [] if isinstance(c, dict)
    }

    def pct(days: int) -> float | None:
        value = (changes.get(days) or {}).get("percent")
        return round(float(value), 2) if isinstance(value, (int, float)) else None

    series = [
        _int(p.get("price")) for p in raw.get("series") or [] if isinstance(p, dict)
    ]
    series = [p for p in series if p is not None]
    return {
        "price": _int(raw["price"]),
        "day": raw.get("day"),
        "change_1d": _int((changes.get(1) or {}).get("change")),
        "pct_1d": pct(1),
        "pct_7d": pct(7),
        "pct_30d": pct(30),
        "high30": _int((raw.get("high30") or {}).get("price")),
        "low30": _int((raw.get("low30") or {}).get("price")),
        "spark": series[-SPARK_DAYS:],
        "checked_at": _iso(_parse(raw.get("checkedAt"))),
    }


class WardogsCollector(Collector):
    name = "wardogs"
    # A hobby site: retry gently (60 s doubling to 30 min) rather than 5 s.
    backoff_start = 60.0
    backoff_max = 1800.0

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        player = str(self.module_config.get("player_id") or DEFAULT_PLAYER).strip()
        if not PLAYER_ID.fullmatch(player):
            raise ValueError(
                f"player_id {player!r} is neither a Steam64 id nor a wdstats player id"
            )
        self.player_id = player
        self.interval = max(MIN_POLL_SECONDS, float(self.module_config.get("poll_seconds", 900)))

    # ---- fetch ---------------------------------------------------------
    async def fetch(self) -> dict:
        problems: list[str] = []
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            response = await client.get(f"{BASE_URL}/api/players/{self.player_id}")
            if response.status_code == 404:
                raise RuntimeError(
                    f"player {self.player_id} isn't tracked on wdstats.co.uk "
                    "— sign in there once with Steam"
                )
            response.raise_for_status()
            player = response.json()
            if not isinstance(player, dict):
                raise ValueError("unexpected player payload")
            try:
                gold = await client.get(f"{BASE_URL}/api/gold")
                gold.raise_for_status()
                data = gold.json()
                if _gold_stage(data) is None:
                    raise ValueError("no price in gold payload")
                _gold["payload"] = data
            except Exception as exc:  # noqa: BLE001 — best effort, keep the last price
                log.debug("wardogs gold failed: %s", exc)
                problems.append(f"gold price unavailable: {type(exc).__name__}")
        if player.get("syncError"):
            problems.append(f"wdstats sync: {player['syncError']}")
        elif player.get("stale"):
            # wdstats' own wording: "Tracking is paused for this player" / "Your game
            # session expired. Sign in again to resume tracking."
            problems.append("wdstats paused tracking (session expired) — sign in again at wdstats.co.uk")
        self.degraded = "; ".join(problems) or None
        return {"player": player, "gold": _gold["payload"]}

    # ---- shape ---------------------------------------------------------
    def _track_levels(self, season: int | None, levels: dict[str, int]) -> list[dict]:
        """Level-ups seen within the last hour. The first poll (and a new
        season, whose levels restart) only records a baseline."""
        now = _now()
        state = _levels.get(self.player_id)
        if state is None or state["season"] != season:
            _levels[self.player_id] = {"season": season, "levels": dict(levels), "ups": []}
            return []
        ups = [u for u in state["ups"] if now - u["at"] < LEVEL_UP_TTL]
        for key, level in levels.items():
            before = state["levels"].get(key)
            if before is not None and level > before:
                ups = [u for u in ups if u["key"] != key]
                ups.append({"at": now, "key": key, "level": level})
        state["levels"] = dict(levels)
        state["ups"] = ups
        return ups

    def shape(self, raw: dict) -> ModulePayload:
        player = raw.get("player") or {}
        gold = _gold_stage(raw.get("gold"))
        name = str(player.get("name") or f"Player {self.player_id[-5:]}").strip()
        season = _int(player.get("season"))
        current = _int(player.get("currentSeason"))
        level = _int(player.get("totalLevel"))
        xp = _int(player.get("totalXp"))
        ranks = player.get("ranks") or {}

        classes = []
        for role in player.get("roles") or []:
            if not isinstance(role, dict) or _int(role.get("level")) is None:
                continue
            key = str(role.get("role") or role.get("label") or "")
            classes.append({
                "key": key,
                "label": str(role.get("label") or key),
                "icon": ROLE_ICONS.get(key, "wardogs"),
                "level": _int(role.get("level")),
                "xp": _int(role.get("xp")) or 0,
                "rank": _int(role.get("rank")),
                "total": _int(role.get("total")),
                "up": False,
            })
        classes.sort(key=lambda c: c["xp"], reverse=True)

        levels = {c["key"]: c["level"] for c in classes}
        if level is not None:
            levels["_total"] = level
        ups = {u["key"]: u for u in self._track_levels(season, levels)}
        for c in classes:
            c["up"] = c["key"] in ups

        money = {
            m.get("currency"): m for m in player.get("money") or [] if isinstance(m, dict)
        }
        cash = money.get("Cash") or {}
        bars = money.get("GoldBars") or {}
        bar_count = _int(bars.get("amount"))
        price = gold["price"] if gold else None

        rarest = sorted(
            (
                i for i in player.get("items") or []
                if isinstance(i, dict) and i.get("category") != "Cosmetics"
                and _int(i.get("owners")) is not None
            ),
            key=lambda i: (i["owners"], str(i.get("name") or "")),
        )[:RAREST]

        now = _now()
        midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        history = player.get("history")
        stage = {
            "player": {
                "id": str(player.get("id") or self.player_id),
                "name": name,
                "avatar": _avatar(player.get("avatarUrl")),
                "url": f"{BASE_URL}/player/{player.get('id') or self.player_id}",
            },
            "season": season,
            "current_season": current,
            "season_final": season is not None and current is not None and season < current,
            "level": level,
            "level_up": "_total" in ups,
            "xp": xp,
            "unlocks": _int(player.get("unlocks")),
            "cosmetics": _int(player.get("cosmetics")),
            "ranks": {
                "level": _rank(ranks.get("level")),
                "xp": _rank(ranks.get("overall")),
                "unlocks": _rank(ranks.get("unlocks")),
            },
            "classes": classes,
            "cash": {"amount": _int(cash.get("amount")), **(_rank(cash) or {})},
            "gold_bars": {
                "count": bar_count,
                **(_rank(bars) or {}),
                "value": bar_count * price if bar_count is not None and price else None,
            },
            "gold": gold,
            "gain": {
                "week": _gain(history, now - timedelta(days=7)),
                "today": _gain(history, midnight),
            },
            "rarest": [
                {"name": str(i.get("name") or ""), "category": i.get("category"),
                 "owners": _int(i.get("owners"))}
                for i in rarest
            ],
            "tracked": _int(player.get("trackedPlayers")),
            "synced_at": _iso(_parse(player.get("profileUpdatedAt"))),
            "sync_stale": bool(player.get("stale")),
            "sync_error": player.get("syncError") or None,
            "hidden": bool(player.get("hidden")),
            "pending": bool(player.get("pending")),
        }
        return ModulePayload(module=self.name, stage=stage, tape=self._tape(stage, ups))

    def _tape(self, stage: dict, ups: dict[str, dict]) -> list[TapeItem]:
        tape: list[TapeItem] = []
        name = stage["player"]["name"]
        by_key = {c["key"]: c for c in stage["classes"]}
        for key, up in ups.items():
            if key in by_key:
                tape.append(TapeItem(
                    text=f"{name} · {by_key[key]['label']} → LVL {up['level']}",
                    accent="up", priority=1, icon=by_key[key]["icon"],
                ))
        if stage["level"] is not None and not stage["hidden"]:
            parts = [name, f"Wardog LVL {stage['level']}"]
            if stage["xp"] is not None:
                parts.append(f"{compact(stage['xp'])} XP")
            rank = stage["ranks"]["xp"]
            if rank:
                parts.append(f"#{rank['rank']}/{rank['total']}" if rank["total"] else f"#{rank['rank']}")
            if stage["gain"]["week"]:
                parts.append(f"+{compact(stage['gain']['week'])} XP this week")
            if stage["season_final"]:
                parts.append(f"S{stage['season']} final")
            tape.append(TapeItem(text=" · ".join(parts), icon="wardogs"))
        gold = stage["gold"]
        if gold:
            change, pct = gold["change_1d"], gold["pct_1d"]
            text = f"Gold bar ${gold['price']:,}"
            accent = "neutral"
            if change and pct is not None:
                text += f" {'▲' if change > 0 else '▼'} {abs(pct):.1f}%"
                accent = "up" if change > 0 else "down"
            tape.append(TapeItem(text=text, accent=accent, icon="wd-gold"))
        return tape
