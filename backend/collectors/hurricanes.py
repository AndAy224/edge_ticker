"""Hurricane tracker — NHC CurrentStorms.json (keyless), Atlantic basin.

Publishes active storms with their forecast track points and cone polygon,
parsed from NHC's per-storm KMZ products (KMZ = zipped KML; stdlib only).
KMZs are re-fetched only when a storm's advisory number changes. Off-season
the stage carries quiet=True and the renderer shows a calm basin map.

Reuses the weather module's coordinates for the distance-from-home readout
(shared receiver position, like airquality/adsb/astro)."""
from __future__ import annotations

import io
import logging
import math
import zipfile
from xml.etree import ElementTree

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

CLASSIFICATION_TEXT = {
    "TD": "Tropical Depression",
    "TS": "Tropical Storm",
    "HU": "Hurricane",
    "MH": "Major Hurricane",
    "PTC": "Post-Tropical Cyclone",
    "PC": "Potential Cyclone",
    "STD": "Subtropical Depression",
    "STS": "Subtropical Storm",
}

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

MAX_CONE_POINTS = 120


def compass(degrees: float | None) -> str:
    if degrees is None:
        return ""
    return COMPASS[round(degrees / 22.5) % 16]


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (
        math.sin((rlat2 - rlat1) / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin((rlon2 - rlon1) / 2) ** 2
    )
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def saffir_simpson(knots: float) -> str:
    """Short category label from sustained wind (kt)."""
    if knots >= 137:
        return "CAT 5"
    if knots >= 113:
        return "CAT 4"
    if knots >= 96:
        return "CAT 3"
    if knots >= 83:
        return "CAT 2"
    if knots >= 64:
        return "CAT 1"
    if knots >= 34:
        return "TS"
    return "TD"


# ---- KML parsing (namespace-agnostic: match on local tag names) --------------

def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _iter_tag(root: ElementTree.Element, tag: str):
    for el in root.iter():
        if _local(el.tag) == tag:
            yield el


def _parse_coords(text: str) -> list[list[float]]:
    """KML 'lon,lat[,alt]' whitespace-separated → [[lat, lon], ...]."""
    points = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) >= 2:
            points.append([float(parts[1]), float(parts[0])])
    return points


def kml_root_from_kmz(content: bytes) -> ElementTree.Element:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".kml"))
        return ElementTree.fromstring(zf.read(name))


def parse_track_kmz(content: bytes) -> list[dict]:
    """Forecast positions: Placemarks holding a Point, in document order."""
    points = []
    for placemark in _iter_tag(kml_root_from_kmz(content), "Placemark"):
        point_el = next(_iter_tag(placemark, "Point"), None)
        if point_el is None:
            continue
        coords_el = next(_iter_tag(point_el, "coordinates"), None)
        coords = _parse_coords(coords_el.text if coords_el is not None else "")
        if not coords:
            continue
        name_el = next(_iter_tag(placemark, "name"), None)
        points.append({
            "lat": coords[0][0],
            "lon": coords[0][1],
            "label": (name_el.text or "").strip() if name_el is not None else "",
        })
    return points


def parse_cone_kmz(content: bytes) -> list[list[float]]:
    """First polygon ring, downsampled to a renderable size."""
    for polygon in _iter_tag(kml_root_from_kmz(content), "Polygon"):
        coords_el = next(_iter_tag(polygon, "coordinates"), None)
        ring = _parse_coords(coords_el.text if coords_el is not None else "")
        if len(ring) >= 3:
            step = max(1, len(ring) // MAX_CONE_POINTS)
            return ring[::step]
    return []


class HurricanesCollector(Collector):
    name = "hurricanes"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 600))
        weather = config.get("modules", {}).get("weather", {})
        self.location_name = self.module_config.get(
            "location_name", weather.get("location_name", "Clearwater, FL")
        )
        self.latitude = self.module_config.get(
            "latitude", weather.get("latitude", 27.9659)
        )
        self.longitude = self.module_config.get(
            "longitude", weather.get("longitude", -82.8001)
        )
        # KMZ products re-fetched only when the advisory number changes:
        # storm id -> (advNum, parsed geometry)
        self._geometry_cache: dict[str, tuple[str, dict]] = {}

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(CURRENT_STORMS_URL)
            response.raise_for_status()
            raw_storms = response.json().get("activeStorms") or []
            storms = []
            for raw in raw_storms:
                storm_id = str(raw.get("id", "")).lower()
                if not storm_id.startswith("al"):
                    continue  # Atlantic basin only — this is a Florida HUD
                raw["_geometry"] = await self._geometry(client, storm_id, raw)
                storms.append(raw)
        # Drop cache entries for dissipated storms.
        live = {str(s.get("id", "")).lower() for s in storms}
        for key in list(self._geometry_cache):
            if key not in live:
                del self._geometry_cache[key]
        return storms

    async def _geometry(self, client: httpx.AsyncClient, storm_id: str, raw: dict) -> dict:
        adv = str((raw.get("forecastTrack") or {}).get("advNum", ""))
        cached = self._geometry_cache.get(storm_id)
        if cached and cached[0] == adv:
            return cached[1]
        geometry: dict = {"track": [], "cone": []}
        for key, parser, out in (
            ("forecastTrack", parse_track_kmz, "track"),
            ("trackCone", parse_cone_kmz, "cone"),
        ):
            url = (raw.get(key) or {}).get("kmzFile")
            if not url:
                continue
            try:
                response = await client.get(url)
                response.raise_for_status()
                geometry[out] = parser(response.content)
            except Exception as exc:  # a missing product must not kill the poll
                log.warning("hurricanes: %s %s fetch/parse failed: %s", storm_id, key, exc)
        self._geometry_cache[storm_id] = (adv, geometry)
        return geometry

    def shape(self, raw: list[dict]) -> ModulePayload:
        storms = []
        tape: list[TapeItem] = []
        for s in raw:
            lat = s.get("latitudeNumeric")
            lon = s.get("longitudeNumeric")
            if lat is None or lon is None:
                continue
            knots = float(s.get("intensity") or 0)
            classification = str(s.get("classification") or "").upper()
            distance = haversine_miles(self.latitude, self.longitude, lat, lon)
            direction_from_home = compass(
                bearing_degrees(self.latitude, self.longitude, lat, lon)
            )
            geometry = s.get("_geometry") or {}
            storm = {
                "id": s.get("id"),
                "name": s.get("name"),
                "class": classification,
                "class_text": CLASSIFICATION_TEXT.get(classification, classification),
                "category": saffir_simpson(knots),
                "wind_mph": round(knots * 1.15078),
                "pressure_mb": s.get("pressure"),
                "lat": lat,
                "lon": lon,
                "movement_dir": compass(s.get("movementDir")),
                "movement_mph": s.get("movementSpeed"),
                "distance_mi": round(distance),
                "bearing_from_home": direction_from_home,
                "advisory_time": s.get("lastUpdate"),
                "track": geometry.get("track") or [],
                "cone": geometry.get("cone") or [],
            }
            storms.append(storm)
            tape.append(TapeItem(
                text=(
                    f"{storm['category']} {storm['name']} — {storm['wind_mph']} mph, "
                    f"{storm['distance_mi']} mi {direction_from_home}, "
                    f"moving {storm['movement_dir']} {storm['movement_mph']} mph"
                ),
                accent="alert",
                priority=5,
            ))
        storms.sort(key=lambda s: s["distance_mi"])
        return ModulePayload(
            module=self.name,
            stage={
                "storms": storms,
                "quiet": not storms,
                "home": {"lat": self.latitude, "lon": self.longitude},
                "location_name": self.location_name,
            },
            tape=tape,
        )
