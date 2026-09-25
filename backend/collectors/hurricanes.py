"""Hurricane tracker — NHC CurrentStorms.json (keyless), Atlantic basin.

Publishes active storms with their forecast track points and cone polygon,
parsed from NHC's per-storm KMZ products (KMZ = zipped KML; stdlib only).
KMZs are re-fetched only when a storm's advisory number changes.

Also publishes the 7-day Graphical Tropical Weather Outlook: the areas NHC is
watching for formation, with their 2-/7-day odds, parsed from the outlook
shapefile bundle (stdlib shapefile/dBASE reader below). Before, a season with
no named storm showed an empty map even while NHC gave a disturbance 70%.
The stage is `quiet` only when there are neither storms nor outlook areas.

A storm whose forecast cone contains home raises `threat` — the display can
auto-feature the module on it (modules.hurricanes.auto_feature).

Reuses the weather module's coordinates for the distance-from-home readout
(shared receiver position, like airquality/adsb/astro)."""
from __future__ import annotations

import io
import logging
import math
import re
import struct
import time
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
# Graphical Tropical Weather Outlook, all basins (areas / X points / motion
# arrows as shapefiles, plus the text outlook). NHC issues it every 6 hours.
OUTLOOK_URL = "https://www.nhc.noaa.gov/xgtwo/gtwo_shapefiles.zip"
OUTLOOK_REFRESH_SECONDS = 1800.0
MAX_OUTLOOK_POINTS = 80

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


def movement_phrase(direction: str, mph: float | None) -> str:
    """Storm-motion readout: NHC omits a motion fix (None) on new systems,
    and reports 0 mph when a storm is stationary."""
    if mph is None:
        return "movement TBD"
    if mph == 0:
        return "stationary"
    return f"moving {direction} {mph} mph"


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


# ---- Outlook shapefiles (ESRI shapefile + dBASE, the subset NHC uses) --------


def _dbf_rows(data: bytes) -> list[dict[str, str]]:
    count, header_len, record_len = struct.unpack("<4xIHH", data[:12])
    fields = []
    for offset in range(32, header_len - 1, 32):
        desc = data[offset : offset + 32]
        if desc[0] == 0x0D:
            break
        fields.append((desc[:11].split(b"\0")[0].decode("ascii"), desc[16]))
    rows = []
    for i in range(count):
        pos = header_len + i * record_len + 1  # skip the deletion flag
        row = {}
        for name, size in fields:
            row[name] = data[pos : pos + size].decode("latin-1").strip()
            pos += size
        rows.append(row)
    return rows


def _shp_records(data: bytes) -> list[list[list[list[float]]] | None]:
    """Each record as a list of parts, each part [[lat, lon], ...] (a point
    is one part of one position); None for a null shape."""
    records: list = []
    pos = 100  # fixed file header
    while pos + 8 <= len(data):
        _, words = struct.unpack(">ii", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + words * 2]
        pos += 8 + words * 2
        shape_type = struct.unpack("<i", body[:4])[0]
        if shape_type == 0:
            records.append(None)
        elif shape_type == 1:  # point
            x, y = struct.unpack("<dd", body[4:20])
            records.append([[[y, x]]])
        elif shape_type in (3, 5):  # polyline, polygon
            n_parts, n_points = struct.unpack("<ii", body[36:44])
            starts = list(struct.unpack(f"<{n_parts}i", body[44 : 44 + 4 * n_parts]))
            coords = struct.unpack(
                f"<{2 * n_points}d", body[44 + 4 * n_parts : 44 + 4 * n_parts + 16 * n_points]
            )
            points = [[coords[2 * k + 1], coords[2 * k]] for k in range(n_points)]
            bounds = starts + [n_points]
            records.append([points[bounds[k] : bounds[k + 1]] for k in range(n_parts)])
        else:
            records.append(None)
    return records


def _percent(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else None


def _thin(points: list[list[float]], limit: int) -> list[list[float]]:
    step = max(1, math.ceil(len(points) / limit))
    return [[round(p[0], 3), round(p[1], 3)] for p in points[::step]]


def parse_outlook_zip(content: bytes, basin: str = "Atlantic") -> dict:
    """{issued, areas: [{area, title, invest, prob2, risk2, prob7, risk7,
    polygon, x, path}]} for one basin of the outlook bundle."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    names = zf.namelist()

    def layer(kind: str) -> list[tuple[dict, list | None]]:
        base = next((n[:-4] for n in names if n.startswith(f"gtwo_{kind}_") and n.endswith(".shp")), None)
        if base is None:
            return []
        return list(zip(_dbf_rows(zf.read(base + ".dbf")), _shp_records(zf.read(base + ".shp"))))

    issued = None
    stamp = next(
        (m for n in names if n.startswith("gtwo_areas_") and (m := re.search(r"_(\d{12})\.shp$", n))),
        None,
    )
    if stamp:  # file stamps are UTC
        issued = datetime.strptime(stamp.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc).isoformat()

    # Area titles from the text outlook: "1. Southwestern Atlantic (AL94):"
    titles: dict[str, tuple[str, str | None]] = {}
    text_name = next((n for n in names if n.startswith("two_atl_text")), None)
    if basin == "Atlantic" and text_name:
        for m in re.finditer(r"^(\d+)\.\s+(.+?):\s*$", zf.read(text_name).decode("latin-1"), re.M):
            invest = re.search(r"\((\w{2}\d{2})\)", m.group(2))
            title = re.sub(r"\s*\(\w{2}\d{2}\)", "", m.group(2)).strip()
            titles.setdefault(m.group(1), (title, invest.group(1) if invest else None))

    xs = {r["AREA"]: shape[0][0] for r, shape in layer("points") if shape and r.get("BASIN") == basin}
    paths = {r["AREA"]: shape[0] for r, shape in layer("lines") if shape and r.get("BASIN") == basin}
    areas = []
    for row, shape in layer("areas"):
        if row.get("BASIN") != basin or not shape:
            continue
        ring = max(shape, key=len)  # outer ring
        title, invest = titles.get(row["AREA"], (None, None))
        areas.append({
            "area": row["AREA"],
            "title": title,
            "invest": invest,
            "prob2": _percent(row.get("PROB2DAY", "")),
            "risk2": row.get("RISK2DAY") or None,
            "prob7": _percent(row.get("PROB7DAY", "")),
            "risk7": row.get("RISK7DAY") or None,
            "polygon": _thin(ring, MAX_OUTLOOK_POINTS),
            "x": ({"lat": xs[row["AREA"]][0], "lon": xs[row["AREA"]][1]} if row["AREA"] in xs else None),
            "path": _thin(paths[row["AREA"]], 20) if row["AREA"] in paths else [],
        })
    areas.sort(key=lambda a: -(a["prob7"] or 0))
    return {"issued": issued, "areas": areas}


def point_in_polygon(lat: float, lon: float, ring: list[list[float]]) -> bool:
    """Ray casting over [[lat, lon], ...] — fine at cone scale (no antimeridian)."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class HurricanesCollector(Collector):
    name = "hurricanes"
    uses_location = True

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
        self._outlook: dict = {"issued": None, "areas": []}
        self._outlook_at = 0.0  # monotonic time of the last outlook attempt
        self._outlook_modified: str | None = None
        self._outlook_error: str | None = None
        self._kmz_failures = 0

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(CURRENT_STORMS_URL)
            response.raise_for_status()
            raw_storms = response.json().get("activeStorms") or []
            storms = []
            self._kmz_failures = 0  # this poll's; a recovered product clears it
            for raw in raw_storms:
                storm_id = str(raw.get("id", "")).lower()
                if not storm_id.startswith("al"):
                    continue  # Atlantic basin only — this is a Florida HUD
                raw["_geometry"] = await self._geometry(client, storm_id, raw)
                storms.append(raw)
            await self._refresh_outlook(client)
        # Drop cache entries for dissipated storms.
        live = {str(s.get("id", "")).lower() for s in storms}
        for key in list(self._geometry_cache):
            if key not in live:
                del self._geometry_cache[key]
        problems = []
        if self._kmz_failures:
            problems.append(f"{self._kmz_failures} storm geometry product(s) failed")
        if self._outlook_error:
            problems.append(f"outlook: {self._outlook_error}")
        self.degraded = "; ".join(problems) or None
        return storms

    async def _refresh_outlook(self, client: httpx.AsyncClient) -> None:
        """The outlook changes every 6 hours; poll it at most every 30 minutes,
        conditionally. A failure keeps the previous outlook."""
        if time.monotonic() - self._outlook_at < OUTLOOK_REFRESH_SECONDS and self._outlook_at:
            return
        self._outlook_at = time.monotonic()
        headers = {"If-Modified-Since": self._outlook_modified} if self._outlook_modified else {}
        try:
            response = await client.get(OUTLOOK_URL, headers=headers)
            if response.status_code == 304:
                self._outlook_error = None
                return
            response.raise_for_status()
            self._outlook = parse_outlook_zip(response.content)
            self._outlook_modified = response.headers.get("last-modified")
            self._outlook_error = None
        except Exception as exc:  # the outlook is extra; storms still publish
            self._outlook_error = f"{type(exc).__name__}: {exc}"
            log.warning("hurricanes: outlook fetch/parse failed: %s", exc)

    async def _geometry(self, client: httpx.AsyncClient, storm_id: str, raw: dict) -> dict:
        adv = str((raw.get("forecastTrack") or {}).get("advNum", ""))
        cached = self._geometry_cache.get(storm_id)
        if cached and cached[0] == adv:
            return cached[1]
        geometry: dict = {"track": [], "cone": []}
        failed = False
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
                failed = True
                log.warning("hurricanes: %s %s fetch/parse failed: %s", storm_id, key, exc)
        if failed:
            # Not cached: caching a failure kept the cone off the map until the
            # next advisory, 3-6 hours later. Retried on the next poll instead.
            self._kmz_failures += 1
        else:
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
            cone = geometry.get("cone") or []
            track = geometry.get("track") or []
            in_cone = len(cone) >= 3 and point_in_polygon(self.latitude, self.longitude, cone)
            closest = min(
                (haversine_miles(self.latitude, self.longitude, p["lat"], p["lon"])
                 for p in [{"lat": lat, "lon": lon}, *track]),
                default=distance,
            )
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
                "track": track,
                "cone": cone,
                "home_in_cone": in_cone,
                "closest_mi": round(closest),
            }
            storms.append(storm)
            if in_cone:
                place = self.location_name.split(",")[0]
                tape.append(TapeItem(
                    text=f"{place} is inside the forecast cone of {storm['class_text']} {storm['name']}",
                    accent="alert",
                    priority=6,
                    icon="warning",
                ))
            tape.append(TapeItem(
                text=(
                    f"{storm['category']} {storm['name']} — {storm['wind_mph']} mph, "
                    f"{storm['distance_mi']} mi {direction_from_home}, "
                    f"{movement_phrase(storm['movement_dir'], storm['movement_mph'])}"
                ),
                accent="alert",
                priority=5,
            ))
        storms.sort(key=lambda s: s["distance_mi"])

        outlook = []
        for area in self._outlook.get("areas") or []:
            anchor = area.get("x") or (
                {"lat": area["polygon"][0][0], "lon": area["polygon"][0][1]} if area.get("polygon") else None
            )
            entry = dict(area)
            if anchor:
                entry["distance_mi"] = round(
                    haversine_miles(self.latitude, self.longitude, anchor["lat"], anchor["lon"])
                )
                entry["bearing_from_home"] = compass(
                    bearing_degrees(self.latitude, self.longitude, anchor["lat"], anchor["lon"])
                )
            entry["home_in_area"] = len(area.get("polygon") or []) >= 3 and point_in_polygon(
                self.latitude, self.longitude, area["polygon"]
            )
            outlook.append(entry)
            if (area.get("prob7") or 0) >= 40:
                where = area.get("title") or f"area {area['area']}"
                tape.append(TapeItem(
                    text=f"NHC: {area['prob7']}% chance of development in 7 days — {where}"
                    + (f", {entry['distance_mi']} mi {entry['bearing_from_home']}" if anchor else ""),
                    accent="alert" if (area.get("prob7") or 0) >= 60 else "neutral",
                    priority=4,
                ))

        threat = next(
            ({"kind": "cone", "storm": s["name"], "class_text": s["class_text"]}
             for s in storms if s["home_in_cone"]),
            None,
        )
        return ModulePayload(
            module=self.name,
            stage={
                "storms": storms,
                "outlook": outlook,
                "outlook_issued": self._outlook.get("issued"),
                "threat": threat,
                "quiet": not storms and not outlook,
                "home": {"lat": self.latitude, "lon": self.longitude},
                "location_name": self.location_name,
            },
            tape=tape,
        )
