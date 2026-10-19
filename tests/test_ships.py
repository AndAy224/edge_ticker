"""backend/collectors/ships.py against 3 min of recorded aisstream.io traffic."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.collectors import ships
from backend.collectors.adsb import haversine_km
from helpers import FIXTURES, until

HOME = (27.9659, -82.8001)  # the shipped default (Clearwater); the recording's box covers its range
RECORDING = FIXTURES / "aisstream_tampabay.jsonl"


def frames() -> list[str]:
    return RECORDING.read_text(encoding="utf-8").splitlines()


def stamp(message: dict) -> datetime | None:
    raw = (message.get("MetaData") or {}).get("time_utc")  # "2026-10-19 13:45:31.430875766 +0000 UTC"
    return datetime.strptime(raw[:26], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc) if raw else None


def replay() -> datetime:
    """Ingest the recording at its own timestamps; returns the last one."""
    last = None
    for line in frames():
        message = json.loads(line)
        last = stamp(message) or last or datetime(2026, 10, 19, 13, 45, 30, tzinfo=timezone.utc)
        ships.ingest(message, last)
    return last


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 10, 19, 13, 48, 30, tzinfo=timezone.utc)}
    monkeypatch.setattr(ships, "_now", lambda: now["t"])
    return now


@pytest.fixture
def collector(defaults_config, monkeypatch, clock) -> ships.ShipsCollector:
    monkeypatch.setenv("AISSTREAM_API_KEY", "test-key")
    defaults_config["modules"]["ships"]["enabled"] = True
    defaults_config["modules"]["weather"].update(latitude=HOME[0], longitude=HOME[1])
    return ships.ShipsCollector(defaults_config)


def position(mmsi: int, lat: float, lon: float, sog: float = 12.0, **extra) -> dict:
    body = {"UserID": mmsi, "Valid": True, "Latitude": lat, "Longitude": lon, "Sog": sog,
            "Cog": 90.0, "TrueHeading": 91, "NavigationalStatus": 0, **extra}
    return {"MessageType": "PositionReport", "MetaData": {"MMSI": mmsi, "ShipName": "TEST    "},
            "Message": {"PositionReport": body}}


def static(mmsi: int, **body) -> dict:
    return {"MessageType": "ShipStaticData", "MetaData": {"MMSI": mmsi},
            "Message": {"ShipStaticData": {"UserID": mmsi, "Valid": True, **body}}}


def test_replay_recorded_traffic(collector, clock):
    clock["t"] = replay()
    assert ships._stream["compression"] is True  # permessage-deflate took
    assert len(ships._vessels) == 71

    payload = collector.shape(asyncio.run(collector.fetch()))
    stage = payload.stage
    vessels = stage["vessels"]
    in_circle = [
        v for v in ships._vessels.values()
        if "lat" in v and haversine_km(*HOME, v["lat"], v["lon"]) <= 50
    ]
    assert stage["count_in_radius"] == len(in_circle) == 55  # the box's corners fall outside
    assert len(vessels) == ships.MAX_VESSELS
    assert stage["center"] == {"lat": HOME[0], "lon": HOME[1]} and stage["radius_km"] == 50

    # Under way first (nearest first), then the moored and anchored.
    flags = [not v["moving"] for v in vessels]
    assert flags == sorted(flags)
    moving = [v for v in vessels if v["moving"]]
    assert stage["count_moving"] == len(moving) == 6
    assert [v["distance_km"] for v in moving] == sorted(v["distance_km"] for v in moving)
    for v in vessels:
        assert v["name"] == v["name"].strip() and "@" not in v["name"]
        assert v["direction"] in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
        if v["moving"]:
            assert v["speed_kn"] >= ships.MOVING_KNOTS

    # A tug's ShipStaticData merged into its position (unavailable ETA dropped).
    liberty = ships._vessels[367174670]
    assert liberty["name"] == "LIBERTY" and liberty["type"] == 52
    assert liberty["length_m"] == 29 and liberty["beam_m"] == 10 and liberty["imo"] == 9482598
    assert liberty["destination"] == "TAMPA" and liberty["draught_m"] == 4.2
    assert ships.eta_of(liberty["eta_raw"], clock["t"]) is None

    # Class B's "not available" course and heading become None.
    singularity = ships._vessels[338532232]
    assert singularity["ais_class"] == "B" and singularity["cog"] is None and singularity["heading"] is None

    # The ticker only carries boats doing 3 kn or more, no "vessel" filler label.
    assert payload.tape and len(payload.tape) <= 3
    for item in payload.tape:
        assert item.icon == "ship" and " · vessel · " not in item.text
        knots = float(item.text.split(" kn")[0].split(" · ")[-1])
        assert knots >= ships.TAPE_KNOTS


def test_ingest_ignores_unavailable_values_and_non_ships():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    ships.ingest(position(367000001, 91.0, 181.0), now)  # no fix
    ships.ingest(position(993669001, 27.9, -82.8), now)  # aid to navigation
    ships.ingest(position(3669001, 27.9, -82.8), now)  # coast station (00366…)
    ships.ingest(position(367000002, 27.9, -82.8, Valid=False), now)
    assert all("lat" not in v for v in ships._vessels.values())
    assert set(ships._vessels) <= {367000001}

    ships.ingest(position(367000003, 27.9, -82.8, sog=102.3, Cog=360, TrueHeading=511,
                          NavigationalStatus=15), now)
    vessel = ships._vessels[367000003]
    assert (vessel["sog"], vessel["cog"], vessel["heading"], vessel["status"]) == (None, None, None, None)
    assert vessel["meta_name"] == "TEST"


def test_class_b_static_reports():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    mmsi = 338000001
    report = {"MessageType": "StaticDataReport", "MetaData": {"MMSI": mmsi},
              "Message": {"StaticDataReport": {"UserID": mmsi, "Valid": True, "PartNumber": False,
                                               "ReportA": {"Valid": True, "Name": "SEA BREEZE@@@"},
                                               "ReportB": {"Valid": False}}}}
    ships.ingest(report, now)
    report["Message"]["StaticDataReport"].update(
        PartNumber=True, ReportA={"Valid": False},
        ReportB={"Valid": True, "ShipType": 36, "CallSign": "WDX1234",
                 "Dimension": {"A": 6, "B": 6, "C": 2, "D": 2}},
    )
    ships.ingest(report, now)
    extended = {"MessageType": "ExtendedClassBPositionReport", "MetaData": {"MMSI": 338000002},
                "Message": {"ExtendedClassBPositionReport": {
                    "UserID": 338000002, "Valid": True, "Latitude": 27.95, "Longitude": -82.8,
                    "Sog": 6.0, "Cog": 200.0, "TrueHeading": 511, "Name": "KNOT NOW", "Type": 37,
                    "Dimension": {"A": 0, "B": 0, "C": 0, "D": 0}}}}
    ships.ingest(extended, now)
    assert ships._vessels[mmsi] == {
        "mmsi": mmsi, "heard": now, "name": "SEA BREEZE", "type": 36, "callsign": "WDX1234",
        "length_m": 12, "beam_m": 4,
    }
    knot = ships._vessels[338000002]
    assert knot["name"] == "KNOT NOW" and knot["type"] == 37 and "length_m" not in knot
    assert knot["ais_class"] == "B" and knot["status"] is None


def test_eta_year_and_staleness():
    now = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)
    assert ships.eta_of({"Month": 1, "Day": 2, "Hour": 6, "Minute": 30}, now) == "2027-01-02T06:30:00Z"
    assert ships.eta_of({"Month": 12, "Day": 31, "Hour": 16, "Minute": 0}, now) == "2026-12-31T16:00:00Z"
    assert ships.eta_of({"Month": 12, "Day": 31, "Hour": 10, "Minute": 0}, now) is None  # 10 h ago
    assert ships.eta_of({"Month": 6, "Day": 1, "Hour": 0, "Minute": 0}, now) is None  # 5 months out
    assert ships.eta_of({"Month": 0, "Day": 0, "Hour": 24, "Minute": 60}, now) is None
    assert ships.eta_of({"Month": 2, "Day": 30, "Hour": 0, "Minute": 0}, now) is None
    assert ships.eta_of(None, now) is None
    assert ships.eta_of({"Month": 1, "Day": 1, "Hour": 0, "Minute": 0}, now) is None  # unset default


def test_destination_cleanup():
    assert ships._destination("TAMPA FL@@@@   ") == "TAMPA FL"
    assert ships._destination("US^0SHG>TAMPA") == "TAMPA"  # previous>next: the next port
    assert ships._destination("XX XXX>US^0SBN") == "US^0SBN"
    assert ships._destination("XX XXX") is None
    assert ships._destination("TAMPA>") is None
    assert ships._destination("") is None and ships._destination(None) is None


def test_kinds_and_flags():
    assert ships.kind_of(70) == ("cargo", "cargo")
    assert ships.kind_of(84) == ("tanker", "tanker")
    assert ships.kind_of(69) == ("passenger", "passenger")
    assert ships.kind_of(52) == ("tug", "tug")
    assert ships.kind_of(55) == ("patrol", "law enforcement")
    assert ships.kind_of(37) == ("pleasure", "pleasure craft")
    assert ships.kind_of(99) == ("other", "vessel")
    assert ships.kind_of(None) == ships.kind_of(0) == ("unknown", "vessel")
    assert ships.flag_of(636019825) == ("LR", "Liberia")
    assert ships.flag_of(367466650) == ("US", "United States")
    assert ships.flag_of(999999999) is None


def test_shape_tape_and_pruning(collector, clock):
    now = clock["t"]
    ships.ingest(position(367000012, 28.0, -82.8, sog=9.0), now - timedelta(minutes=16))  # gone quiet
    ships.ingest(position(636000001, 27.92, -82.85, sog=12.4), now)  # ~7 km SW of home
    ships.ingest(static(636000001, Name="GULF TRADER", Type=70, Destination="US TPA@@@",
                        Dimension={"A": 150, "B": 30, "C": 14, "D": 14}, MaximumStaticDraught=9.5,
                        Eta={"Month": 10, "Day": 19, "Hour": 18, "Minute": 0}), now)
    ships.ingest(position(367000010, 28.05, -82.72, sog=0.0, NavigationalStatus=5), now)  # moored
    ships.ingest(position(367000011, 28.10, -82.70, sog=0.8), now)  # GPS jitter
    ships._vessels[367000013] = {"mmsi": 367000013, "heard": now - timedelta(minutes=20), "name": "X"}

    payload = collector.shape(asyncio.run(collector.fetch()))
    names = [v["name"] for v in payload.stage["vessels"]]
    assert names[0] == "GULF TRADER" and len(names) == 3
    assert 367000012 not in ships._vessels  # pruned from the table, not just the payload
    assert 367000013 not in ships._vessels  # static data only, never a position
    trader = payload.stage["vessels"][0]
    assert trader["flag"] == "LR" and trader["kind"] == "cargo" and trader["moving"]
    assert (trader["length_m"], trader["beam_m"], trader["draught_m"]) == (180, 28, 9.5)
    assert trader["destination"] == "US TPA" and trader["eta"] == "2026-10-19T18:00:00Z"
    assert payload.stage["count_moving"] == 1
    assert [v["status"] for v in payload.stage["vessels"][1:]] == ["moored", "under way"]
    assert [t.text for t in payload.tape] == ["GULF TRADER · cargo · 12 kn → US TPA · 7 km SW"]


async def test_fetch_warming_silence_and_compression(collector, clock):
    first = await collector.fetch()  # just started, nothing heard yet: an empty, warming sea
    assert first == {"vessels": [], "warming": True}

    ships._stream["error"] = "ConnectionClosedError: 1006"
    with pytest.raises(RuntimeError, match="ConnectionClosedError"):
        await collector.fetch()  # an error and still nothing heard: say why
    ships._stream["error"] = None

    ships.ingest(position(367000020, 28.0, -82.7), clock["t"])
    ships._stream["compression"] = False
    raw = await collector.fetch()
    assert len(raw["vessels"]) == 1 and raw["warming"]
    assert collector.degraded == "AIS stream uncompressed — aisstream caps its bandwidth"
    ships._stream.update(compression=True, error="ConnectionClosedError: 1006")
    await collector.fetch()  # fresh data still on hand: degraded, not failed
    assert collector.degraded == "AIS stream reconnecting: ConnectionClosedError: 1006"
    ships._stream["error"] = None

    clock["t"] += ships.WARMUP
    ships.ingest(position(367000020, 28.0, -82.7), clock["t"])
    assert (await collector.fetch())["warming"] is False
    clock["t"] += ships.STREAM_SILENT + timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="no AIS messages"):
        await collector.fetch()


class FakeSocket:
    def __init__(self, frames: list, close_code: int = 1000, close_reason: str = "", hang: bool = False):
        self.frames, self.sent, self.hang = list(frames), [], hang
        self.close_code, self.close_reason = close_code, close_reason

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        for frame in self.frames:
            yield frame
            await asyncio.sleep(0)
        if self.hang:
            await asyncio.Event().wait()


@pytest.fixture
def fake_stream(monkeypatch):
    calls: list[dict] = []
    sockets: list[FakeSocket] = []
    delays: list[float] = []

    def connect(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return sockets.pop(0) if sockets else FakeSocket([], hang=True)

    async def no_sleep(seconds):
        delays.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(ships.websockets, "connect", connect)
    monkeypatch.setattr(ships, "_sleep", no_sleep)
    return calls, sockets, delays


async def test_stream_subscribes_ingests_and_reconnects(collector, fake_stream):
    calls, sockets, delays = fake_stream
    recorded = [line.encode() for line in frames()]  # aisstream sends binary frames
    first = FakeSocket(recorded, close_code=1006, close_reason="")
    sockets.append(first)
    task = asyncio.create_task(collector._stream())
    try:
        await until(lambda: len(calls) == 2)  # closed after the recording: reconnected
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert calls[0]["url"] == ships.STREAM_URL and calls[0]["compression"] == "deflate"
    [subscription] = first.sent
    assert subscription["APIKey"] == "test-key"
    assert subscription["FilterMessageTypes"] == ships.MESSAGE_TYPES
    [[south_west, north_east]] = subscription["BoundingBoxes"]
    assert south_west[0] < HOME[0] < north_east[0] and south_west[1] < HOME[1] < north_east[1]
    assert haversine_km(HOME[0], HOME[1], north_east[0], HOME[1]) == pytest.approx(50, rel=0.01)
    assert haversine_km(HOME[0], HOME[1], HOME[0], north_east[1]) == pytest.approx(50, rel=0.01)
    assert len(ships._vessels) == 71
    assert ships._stream["error"] == "stream closed by aisstream: 1006"
    assert ships.STREAM_BACKOFF[0] <= delays[0] <= ships.STREAM_BACKOFF[0] * 1.25


async def test_refused_key_backs_off_slowly(collector, fake_stream):
    calls, sockets, delays = fake_stream
    sockets.append(FakeSocket([b'{"error": "Api Key Is Not Valid"}']))
    task = asyncio.create_task(collector._stream())
    try:
        await until(lambda: len(calls) == 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert ships._stream["error"] == "aisstream refused the subscription: Api Key Is Not Valid"
    assert delays[0] >= ships.REJECTED_BACKOFF
    with pytest.raises(RuntimeError, match="Api Key Is Not Valid"):
        await collector.fetch()


async def test_silent_open_stream_reconnects(collector, fake_stream, monkeypatch):
    calls, sockets, delays = fake_stream
    monkeypatch.setattr(ships, "STREAM_SILENT", timedelta(milliseconds=50))
    sockets.append(FakeSocket([frames()[1].encode()], hang=True))  # one message, then nothing
    task = asyncio.create_task(collector._stream())
    try:
        await until(lambda: len(calls) == 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(ships._vessels) == 1
    assert ships._stream["error"] == "no frames for 0 min — reconnecting"
