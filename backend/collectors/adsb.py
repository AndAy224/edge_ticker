"""ADS-B "overhead now" + flight-radar collector.

Default source is a free, keyless hosted community ADS-B API (adsb.fi) —
no receiver hardware required. Switch with ``modules.adsb.provider``:

  adsbfi | adsblol | airplaneslive | local

airplanes.live closed its keyless API in Aug 2026 (403 + a "contact us" body on
every request), so it is kept as an option but is no longer the default.
adsb.lol started rate-limiting in Oct 2026 — 4/s, 60/min and 500 per day per
IP (``x-ratelimit-limit``), which a 15 s poll spends by 02:00 UTC, leaving the
radar stale for the remaining 22 hours — so it is no longer the default either.

A hosted provider that fails hands the poll to the next one (FALLBACK_ORDER),
and is skipped for a while: a 429 until its ``Retry-After`` /
``x-ratelimit-reset``, a 403 for an hour, anything else for two minutes.

The hosted providers all return an ADSBexchange-v2 shape (an ``ac`` array of
dump1090-style aircraft) and take their radius in nautical miles. ``local``
reads ``ADSB_URL`` from .env (e.g. http://pi-adsb:8080/data/aircraft.json) from
your own dump1090/readsb receiver. Receiver/center position defaults to the
weather module's coordinates.
"""
from __future__ import annotations

import email.utils
import math
import os
from datetime import datetime, timedelta, timezone

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

EARTH_RADIUS_KM = 6371.0
KM_PER_NM = 1.852
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
USER_AGENT = "edge-ticker/1.0 (hobby flight-radar kiosk)"

# Hosted community ADS-B providers — keyless, radius in nautical miles. They all
# return the same ADSBexchange-v2 JSON (an `ac` array), so one parser fits all.
HOSTED_PROVIDERS = {
    "airplaneslive": lambda lat, lon, nm: f"https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
    "adsblol": lambda lat, lon, nm: f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}",
    "adsbfi": lambda lat, lon, nm: f"https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}",
}
PROVIDER_LABELS = {"airplaneslive": "airplanes.live", "adsblol": "adsb.lol", "adsbfi": "adsb.fi"}
# Tried after the configured provider. airplanes.live is left out: its keyless
# API has answered 403 to everyone since Aug 2026.
FALLBACK_ORDER = ("adsbfi", "adsblol")
RATE_LIMIT_DEFAULT = timedelta(minutes=15)  # a 429 that names no reset time
RATE_LIMIT_MAX = timedelta(hours=24)
FORBIDDEN_SKIP = timedelta(hours=1)
FAILED_SKIP = timedelta(minutes=2)

# provider -> (skip until, why). Module-level on purpose (the opnsense
# precedent): a config save restarts the collector, and a fresh instance would
# otherwise spend another request on a provider that is locked out for hours.
_skip: dict[str, tuple[datetime, str]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)  # a function so tests can pin the clock


def _rate_limit_reset(response: httpx.Response, now: datetime) -> datetime:
    """When a 429 lifts: Retry-After (seconds or an HTTP date), else
    x-ratelimit-reset (seconds from now, as adsb.lol's envoy sends it — or an
    epoch), else RATE_LIMIT_DEFAULT; clamped to [1 min, RATE_LIMIT_MAX]."""
    wait: timedelta | None = None
    for header in ("retry-after", "x-ratelimit-reset"):
        value = (response.headers.get(header) or "").strip()
        if not value:
            continue
        try:
            seconds = float(value)
        except ValueError:
            try:
                wait = email.utils.parsedate_to_datetime(value) - now
            except (TypeError, ValueError):
                continue
        else:
            wait = timedelta(seconds=seconds - now.timestamp() if seconds > 1e9 else seconds)
        break
    if wait is None:
        wait = RATE_LIMIT_DEFAULT
    return now + min(max(wait, timedelta(minutes=1)), RATE_LIMIT_MAX)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass_octant(deg: float) -> str:
    return COMPASS[round(deg / 45) % 8]


class AdsbCollector(Collector):
    name = "adsb"
    uses_location = True
    enabled_by_default = False
    # Hosted providers need no env; the `local` provider reads ADSB_URL itself.
    required_env = ()

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 15))
        self.provider = (self.module_config.get("provider") or "adsbfi").lower()
        weather = config.get("modules", {}).get("weather", {})
        self.latitude = self.module_config.get("latitude") or weather.get("latitude", 27.9659)
        self.longitude = self.module_config.get("longitude") or weather.get("longitude", -82.8001)
        self.radius_km = float(self.module_config.get("radius_km", 40))
        self.local_url = os.environ.get("ADSB_URL")

    def _url(self, provider: str | None = None) -> str:
        provider = provider or self.provider
        if provider == "local":
            if not self.local_url:
                raise RuntimeError(
                    "adsb provider 'local' needs ADSB_URL in .env "
                    "(e.g. http://pi-adsb:8080/data/aircraft.json)"
                )
            return self.local_url
        builder = HOSTED_PROVIDERS.get(provider)
        if builder is None:
            raise RuntimeError(f"unknown adsb provider {provider!r}")
        nm = max(1, round(self.radius_km / KM_PER_NM))
        return builder(self.latitude, self.longitude, nm)

    def _providers(self) -> list[str]:
        """The configured provider, then the hosted fallbacks. A local
        receiver is never swapped for a hosted feed."""
        if self.provider == "local":
            return ["local"]
        return [self.provider] + [p for p in FALLBACK_ORDER if p != self.provider]

    async def fetch(self) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        now = _now()
        problems: list[str] = []
        error: Exception | None = None
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            for provider in self._providers():
                label = PROVIDER_LABELS.get(provider, provider)
                skip = _skip.get(provider)
                if skip and now < skip[0]:
                    problems.append(skip[1])
                    continue
                url = self._url(provider)  # a config error, not an upstream one: raise
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    raw = response.json()
                except Exception as exc:  # noqa: BLE001 — next provider
                    if provider == "local":
                        raise  # the user's own receiver: no skip window, no stand-in
                    error = exc
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    if status == 429:
                        until = _rate_limit_reset(exc.response, now)
                        why = f"{label} rate-limited until {until:%H:%M} UTC"
                    elif status == 403:
                        until, why = now + FORBIDDEN_SKIP, f"{label} refused (403)"
                    else:
                        until, why = now + FAILED_SKIP, f"{label} failed: {type(exc).__name__}"
                    _skip[provider] = (until, why)
                    problems.append(why)
                    continue
                _skip.pop(provider, None)
                self.degraded = (
                    f"{'; '.join(problems)} — using {label}" if provider != self.provider else None
                )
                return raw
        self.degraded = None  # the error says it all
        if error is None:  # every provider is still inside its skip window
            raise RuntimeError("; ".join(problems))
        if len(problems) > 1:
            raise RuntimeError("; ".join(problems)) from error
        raise error

    def shape(self, raw: dict) -> ModulePayload:
        # Hosted providers key the list under "ac"; local dump1090 uses "aircraft".
        raw_list = raw.get("ac") or raw.get("aircraft") or []
        aircraft = []
        for plane in raw_list:
            lat, lon = plane.get("lat"), plane.get("lon")
            if lat is None or lon is None:
                continue
            distance = haversine_km(self.latitude, self.longitude, lat, lon)
            if distance > self.radius_km:
                continue
            bearing = bearing_deg(self.latitude, self.longitude, lat, lon)
            alt_raw = plane.get("alt_baro")
            on_ground = alt_raw == "ground"
            if on_ground:
                alt_ft = 0
            elif isinstance(alt_raw, (int, float)):
                alt_ft = alt_raw
            else:
                alt_ft = plane.get("alt_geom")
            vert = plane.get("baro_rate")
            if vert is None:
                vert = plane.get("geom_rate")
            aircraft.append(
                {
                    "hex": plane.get("hex"),
                    "flight": (plane.get("flight") or "").strip()
                    or plane.get("r")
                    or plane.get("hex", "?"),
                    "alt_ft": alt_ft,
                    "speed_kt": plane.get("gs"),
                    "track": plane.get("track"),
                    "distance_km": round(distance, 1),
                    "bearing_deg": round(bearing, 1),
                    "direction": compass_octant(bearing),
                    "lat": lat,
                    "lon": lon,
                    "type": plane.get("t"),
                    "registration": plane.get("r"),
                    "squawk": plane.get("squawk"),
                    "category": plane.get("category"),
                    "vert_rate": vert,
                    "on_ground": on_ground,
                    # airplanes.live enriches inline (other providers may omit).
                    "desc": plane.get("desc"),
                    "operator": plane.get("ownOp"),
                }
            )
        # Airborne first, then nearest: within 40 km of an airport the nearest
        # contacts are otherwise a list of parked and taxiing jets.
        aircraft.sort(key=lambda a: (a["on_ground"], a["distance_km"]))

        tape = [
            TapeItem(
                text=f"{a['flight']} · {a['alt_ft'] or '?'} ft · "
                f"{a['distance_km']:.0f} km {a['direction']}"
            )
            for a in aircraft[:3]
            if not a["on_ground"]
        ]
        total = raw.get("total")
        if total is None:
            total = len(raw_list)
        return ModulePayload(
            module=self.name,
            stage={
                "aircraft": aircraft[:30],
                "count_in_radius": len(aircraft),
                "count_airborne": sum(1 for a in aircraft if not a["on_ground"]),
                "count_total": total,
                "radius_km": self.radius_km,
                # receiver position so the display can project a basemap onto the scope
                "center": {"lat": self.latitude, "lon": self.longitude},
            },
            tape=tape,
        )
