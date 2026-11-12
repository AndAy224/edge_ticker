"""backend/collectors/hurricanes.py: a new advisory whose KMZ products are not
up yet keeps the previous advisory's geometry (per product) instead of an empty
cone, while still retrying and reporting the failure."""
from __future__ import annotations

import copy
import io
import zipfile

import httpx

from backend.collectors import base
from backend.collectors.hurricanes import HurricanesCollector
from helpers import json_response, load_json


def kmz(kml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("doc.kml", kml)
    return buffer.getvalue()


def track_kml(*points: tuple[float, float]) -> str:
    marks = "".join(
        f"<Placemark><name>P{i}</name><Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>"
        for i, (lat, lon) in enumerate(points)
    )
    return f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{marks}</Document></kml>'


# Around Clearwater (27.97, -82.80): home is inside this cone.
CONE_KML = """<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><Polygon>
<outerBoundaryIs><LinearRing><coordinates>-86,23 -82,23 -81,29 -84.5,29 -86,23</coordinates>
</LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"""
TRACK_021 = track_kml((24.0, -84.0), (26.0, -83.5))
TRACK_022 = track_kml((24.5, -84.0), (27.0, -83.2))


class Nhc:
    """One Atlantic storm whose advisory can move on before its KMZs exist."""

    def __init__(self) -> None:
        self.adv = "021"
        self.products: dict[str, bytes] = {
            "021adv_TRACK": kmz(TRACK_021), "021adv_CONE": kmz(CONE_KML),
        }
        self.kmz_requests: list[str] = []

    def storms(self) -> dict:
        data = copy.deepcopy(load_json("nhc_current_storms.json"))
        storm = next(s for s in data["activeStorms"] if s["id"] == "al062026")
        storm.update(latitudeNumeric=24.5, longitudeNumeric=-84.0)
        base = "https://www.nhc.noaa.gov/storm_graphics/api/AL062026_"
        storm["forecastTrack"].update(advNum=self.adv, kmzFile=f"{base}{self.adv}adv_TRACK.kmz")
        storm["trackCone"].update(advNum=self.adv, kmzFile=f"{base}{self.adv}adv_CONE.kmz")
        return {"activeStorms": [storm]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("CurrentStorms.json"):
            return json_response(self.storms())
        if not url.endswith(".kmz"):
            return httpx.Response(304)  # outlook: unchanged
        self.kmz_requests.append(url)
        product = url.rsplit("AL062026_", 1)[1].removesuffix(".kmz")
        if product in self.products:
            return httpx.Response(200, content=self.products[product])
        return httpx.Response(404)  # not published yet


async def poll(collector: HurricanesCollector):
    return collector.shape(await collector.fetch()).stage


async def test_new_advisory_without_kmzs_keeps_the_previous_geometry(http, defaults_config):
    nhc = Nhc()
    http.handler = nhc
    collector = HurricanesCollector(defaults_config)  # Clearwater
    stage = await poll(collector)
    previous = stage["storms"][0]
    assert previous["home_in_cone"] and stage["threat"]["storm"] == "Fay"
    assert collector.degraded is None

    # Advisory 022 is out; its track KMZ is up, its cone not yet.
    nhc.adv = "022"
    nhc.products["022adv_TRACK"] = kmz(TRACK_022)
    stage = await poll(collector)
    storm = stage["storms"][0]
    assert storm["cone"] == previous["cone"]  # was [] — and the threat with it
    assert storm["track"] == [{"lat": 24.5, "lon": -84.0, "label": "P0"},
                              {"lat": 27.0, "lon": -83.2, "label": "P1"}]  # the new one
    assert storm["home_in_cone"] and stage["threat"]["storm"] == "Fay"
    assert collector.degraded == "1 storm geometry product(s) failed"
    assert collector._geometry_cache["al062026"][0] == "021"  # the failure isn't cached

    # Next poll retries both products; once the cone is up it all moves on.
    nhc.products["022adv_CONE"] = kmz(CONE_KML)
    requests_before = len(nhc.kmz_requests)
    stage = await poll(collector)
    assert len(nhc.kmz_requests) == requests_before + 2
    assert collector._geometry_cache["al062026"][0] == "022"
    assert collector.degraded is None and stage["threat"]["storm"] == "Fay"


async def test_first_sighting_without_kmzs_is_still_empty(http, defaults_config):
    nhc = Nhc()
    nhc.products.clear()
    http.handler = nhc
    collector = HurricanesCollector(defaults_config)
    stage = await poll(collector)
    assert stage["storms"][0]["cone"] == [] and stage["threat"] is None
    assert collector.degraded == "1 storm geometry product(s) failed"


def test_fetch_deadline_leaves_room_for_a_busy_basin():
    # Products are fetched one after another: CurrentStorms, two per storm on
    # a new advisory, the outlook. Four Atlantic storms with NHC answering at
    # 15 s a request is a slow poll, not a hung one.
    slow_poll = (1 + 2 * 4 + 1) * 15
    assert slow_poll > base.FETCH_TIMEOUT_SECONDS
    assert slow_poll < HurricanesCollector.fetch_timeout < base.HUNG_ATTEMPT_SECONDS
