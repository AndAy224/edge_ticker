"""Ships — live AIS vessel traffic around home, from aisstream.io.

AIS is ADS-B for ships: transponders broadcast MMSI, position, speed, course
and status on marine VHF, plus (every few minutes) name, type, size, draught,
destination and ETA. Class A (commercial ships, required) reports every
2-10 s under way and every 3 min at anchor; Class B (many leisure boats,
voluntary) less often. aisstream.io relays the world's terrestrial receivers
over one WebSocket — free, needs AISSTREAM_API_KEY.

It is a push stream, not a poll: a background task holds the socket (as the
markets collector holds Finnhub's) and folds every message into a vessel
table; the base loop's fetch() only snapshots that table every poll_seconds,
so stale marking, health and the watchdog work unchanged. fetch() raises once
the stream has been silent for STREAM_SILENT.

Stream rules (aisstream docs, Oct 2026): one JSON subscription within 3 s of
connecting, carrying the key (never put it in the URL — URLs get logged) and
a bounding box; at most 3 connections per account and per IP, so dev and prod
fit; binary frames of UTF-8 JSON; permessage-deflate expected — uncompressed
connections are bandwidth-capped since Sept 2026 (websockets negotiates it,
and the SubscriptionConfirmation says whether it took); no SLA and no replay,
so after a restart the table refills over ~3 min.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
from datetime import datetime, timedelta, timezone

import websockets

from ..state import Bus, ModulePayload, TapeItem
from .adsb import bearing_deg, compass_octant, haversine_km
from .base import MIN_INTERVAL_SECONDS, Collector

log = logging.getLogger(__name__)

STREAM_URL = "wss://stream.aisstream.io/v0/stream"
MESSAGE_TYPES = [
    "PositionReport",  # Class A, messages 1-3
    "StandardClassBPositionReport",  # 18
    "ExtendedClassBPositionReport",  # 19: position plus name/type/size
    "ShipStaticData",  # 5: name, type, size, draught, destination, ETA
    "StaticDataReport",  # 24: Class B name (part A) and type/size (part B)
]
POSITION_TYPES = {"PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"}
STREAM_SILENT = timedelta(minutes=5)  # no message this long: the stream is down
WARMUP = timedelta(minutes=6)  # static data (type, size) comes every 6 min
STREAM_BACKOFF = (5.0, 300.0)
REJECTED_BACKOFF = 900.0  # a refused key won't fix itself in 5 s
MAX_VESSELS = 40
TAPE_KINDS = {"cargo", "tanker", "passenger", "patrol", "military"}  # the ones worth a ticker line
# A moored Class B boat's GPS jitter reads 0.5-0.9 kn (seen in the recording).
MOVING_KNOTS = 1.0
TAPE_KNOTS = 3.0
STOPPED_STATUSES = {1, 5, 6}  # at anchor, moored, aground

NAV_STATUS = {
    0: "under way", 1: "at anchor", 2: "not under command", 3: "restricted manoeuvrability",
    4: "constrained by draught", 5: "moored", 6: "aground", 7: "fishing", 8: "sailing",
    11: "towing astern", 12: "pushing ahead", 14: "AIS-SART active",
}

# ITU maritime identification digits (an MMSI's first three) for the flags
# likely in a Gulf port; anything else just shows no flag.
FLAGS: dict[str, tuple[str, str]] = {}
for _mids, _code, _country in (
    ("303 338 366 367 368 369", "US", "United States"),
    ("358", "PR", "Puerto Rico"), ("379", "VI", "US Virgin Islands"),
    ("316", "CA", "Canada"), ("345", "MX", "Mexico"),
    ("351 352 353 354 355 356 357 370 371 372 373 374", "PA", "Panama"),
    ("636 637", "LR", "Liberia"), ("538", "MH", "Marshall Islands"),
    ("308 309 311", "BS", "Bahamas"), ("310", "BM", "Bermuda"),
    ("319", "KY", "Cayman Islands"), ("312", "BZ", "Belize"),
    ("304 305", "AG", "Antigua & Barbuda"), ("375 376 377", "VC", "St Vincent & Grenadines"),
    ("341", "KN", "St Kitts & Nevis"), ("378", "VG", "British Virgin Islands"),
    ("314", "BB", "Barbados"), ("334", "HN", "Honduras"), ("339", "JM", "Jamaica"),
    ("362", "TT", "Trinidad & Tobago"), ("327", "DO", "Dominican Republic"),
    ("323", "CU", "Cuba"), ("306", "CW", "Curaçao"), ("307", "AW", "Aruba"),
    ("215 229 248 249 256", "MT", "Malta"), ("209 210 212", "CY", "Cyprus"),
    ("237 239 240 241", "GR", "Greece"), ("257 258 259", "NO", "Norway"),
    ("219 220", "DK", "Denmark"), ("232 233 234 235", "GB", "United Kingdom"),
    ("236", "GI", "Gibraltar"), ("244 245 246", "NL", "Netherlands"),
    ("211 218", "DE", "Germany"), ("226 227 228", "FR", "France"),
    ("224 225", "ES", "Spain"), ("247", "IT", "Italy"), ("255 263", "PT", "Portugal"),
    ("205", "BE", "Belgium"), ("230", "FI", "Finland"), ("265 266", "SE", "Sweden"),
    ("271", "TR", "Turkey"), ("273", "RU", "Russia"),
    ("563 564 565 566", "SG", "Singapore"), ("477", "HK", "Hong Kong"),
    ("412 413 414", "CN", "China"), ("431 432", "JP", "Japan"),
    ("440 441", "KR", "South Korea"), ("419", "IN", "India"),
    ("533", "MY", "Malaysia"), ("548", "PH", "Philippines"),
    ("503", "AU", "Australia"), ("512", "NZ", "New Zealand"),
    ("710", "BR", "Brazil"), ("730", "CO", "Colombia"), ("725", "CL", "Chile"),
    ("701", "AR", "Argentina"), ("775", "VE", "Venezuela"), ("760", "PE", "Peru"),
):
    for _mid in _mids.split():
        FLAGS[_mid] = (_code, _country)

# Table and stream status are module-level on purpose (the opnsense
# precedent): a config save restarts the collector, and a fresh instance
# would otherwise start from an empty sea and wait minutes for names.
_vessels: dict[int, dict] = {}
_stream: dict = {
    "last_message": None,  # datetime of the last AIS message
    "listening_since": None,  # first AIS message this process saw
    "compression": None,  # what the SubscriptionConfirmation said
    "error": None,  # why the stream last dropped
}


class StreamRejected(Exception):
    """aisstream answered with an error (a bad key, a malformed subscription)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)  # a function so tests can pin the clock


_sleep = asyncio.sleep  # module attribute so tests can see (and skip) the reconnect delay


def _text(value: object) -> str | None:
    """AIS strings are padded with '@' and spaces."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value.replace("@", " ")).strip()
    return cleaned or None


def _destination(value: object) -> str | None:
    """Free text: "TAMPA FL", "US TPA" — or routing like "US^0SHG>TAMPA"
    (previous>next, keep the next port) and placeholders like "XX XXX"."""
    text = _text(value)
    if text and ">" in text:
        text = text.rsplit(">", 1)[1].strip() or None
    if text and not text.replace("X", "").strip():
        return None
    return text


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _meta(meta: dict, key: str) -> object:
    """MetaData keys have changed case between aisstream versions."""
    if key in meta:
        return meta[key]
    lowered = key.lower()
    return next((v for k, v in meta.items() if k.lower() == lowered), None)


def kind_of(type_code: int | None) -> tuple[str, str]:
    """AIS ship type -> (category key for colour, label)."""
    t = type_code or 0
    if 70 <= t <= 79:
        return "cargo", "cargo"
    if 80 <= t <= 89:
        return "tanker", "tanker"
    if 60 <= t <= 69:
        return "passenger", "passenger"
    if 40 <= t <= 49:
        return "highspeed", "high-speed craft"
    labels = {
        30: ("fishing", "fishing"), 31: ("tug", "towing"), 32: ("tug", "towing"),
        52: ("tug", "tug"), 50: ("tug", "pilot"), 53: ("tug", "port tender"),
        33: ("tug", "dredger"), 34: ("tug", "dive vessel"), 54: ("tug", "anti-pollution"),
        58: ("tug", "medical"), 35: ("military", "military"), 51: ("patrol", "search & rescue"),
        55: ("patrol", "law enforcement"), 36: ("sailing", "sailing"), 37: ("pleasure", "pleasure craft"),
    }
    return labels.get(t, ("other", "vessel") if t else ("unknown", "vessel"))


def flag_of(mmsi: int) -> tuple[str, str] | None:
    return FLAGS.get(str(mmsi)[:3])


def _dimensions(dim: object) -> tuple[int | None, int | None]:
    """Length, beam in metres from the A/B/C/D offsets (0 = not reported)."""
    if not isinstance(dim, dict):
        return None, None
    a, b, c, d = (int(_num(dim.get(k)) or 0) for k in "ABCD")
    return (a + b if a and b else None), (c + d if c and d else None)


def eta_of(eta: object, now: datetime) -> str | None:
    """AIS ETA (month/day/hour/minute UTC, no year) -> ISO UTC, picking the
    year that puts it nearest now. Ships often leave a voyage's ETA set long
    after arriving, so one more than 6 hours past (or 90 days ahead) is dropped."""
    if not isinstance(eta, dict):
        return None
    month, day, hour, minute = (int(_num(eta.get(k)) or 0) for k in ("Month", "Day", "Hour", "Minute"))
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60):
        return None
    if (month, day, hour, minute) == (1, 1, 0, 0):
        return None  # a common "unset" default (seen live on a tanker)
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, minute, tzinfo=timezone.utc))
        except ValueError:
            continue
    if not candidates:
        return None
    best = min(candidates, key=lambda c: abs(c - now))
    if not -timedelta(hours=6) <= best - now <= timedelta(days=90):
        return None
    return best.strftime("%Y-%m-%dT%H:%M:%SZ")


def _static(vessel: dict, body: dict) -> None:
    name = _text(body.get("Name"))
    if name:
        vessel["name"] = name
    callsign = _text(body.get("CallSign"))
    if callsign:
        vessel["callsign"] = callsign
    type_code = _num(body.get("Type", body.get("ShipType")))
    if type_code:
        vessel["type"] = int(type_code)
    length, beam = _dimensions(body.get("Dimension"))
    if length:
        vessel["length_m"] = length
    if beam:
        vessel["beam_m"] = beam


def ingest(message: dict, now: datetime) -> None:
    """Fold one aisstream message into the vessel table."""
    if not isinstance(message, dict):
        return
    if message.get("error"):
        raise StreamRejected(str(message["error"]))
    kind = message.get("MessageType")
    envelope = message.get("Message") or {}
    if kind == "SubscriptionConfirmation":
        body = envelope.get(kind, envelope)
        _stream["compression"] = body.get("CompressionEnabled")
        return
    body = envelope.get(kind) or {}
    meta = message.get("MetaData") or {}
    mmsi = _num(body.get("UserID")) or _num(_meta(meta, "MMSI"))
    # Ships only: 9 digits starting with an MID (2-7). Coast stations (00…),
    # SAR aircraft (111…), aids to navigation (99…) and SARTs (97…) aren't.
    if mmsi is None or not 200_000_000 <= mmsi < 800_000_000:
        return
    mmsi = int(mmsi)
    _stream["last_message"] = now
    if _stream["listening_since"] is None:
        _stream["listening_since"] = now
    if body.get("Valid") is False:
        return
    vessel = _vessels.setdefault(mmsi, {"mmsi": mmsi})
    vessel["heard"] = now  # any message; `seen` is the last valid position
    meta_name = _text(_meta(meta, "ShipName"))
    if meta_name:
        vessel["meta_name"] = meta_name

    if kind in POSITION_TYPES:
        lat, lon = _num(body.get("Latitude")), _num(body.get("Longitude"))
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return  # 91/181 mean "not available"
        sog, cog, heading = _num(body.get("Sog")), _num(body.get("Cog")), _num(body.get("TrueHeading"))
        # Class B has no navigational status; 15 is "not defined".
        status = body.get("NavigationalStatus") if kind == "PositionReport" else None
        vessel.update(
            lat=lat,
            lon=lon,
            sog=sog if sog is not None and sog < 102.3 else None,
            cog=cog if cog is not None and cog < 360 else None,
            heading=heading if heading is not None and heading < 360 else None,
            status=status if isinstance(status, int) and status != 15 else None,
            ais_class="A" if kind == "PositionReport" else "B",
            seen=now,
        )
        if kind == "ExtendedClassBPositionReport":
            _static(vessel, body)
    elif kind == "ShipStaticData":
        _static(vessel, body)
        imo = _num(body.get("ImoNumber"))
        if imo:
            vessel["imo"] = int(imo)
        vessel["destination"] = _destination(body.get("Destination"))
        vessel["eta_raw"] = body.get("Eta")
        draught = _num(body.get("MaximumStaticDraught"))
        vessel["draught_m"] = draught or None
    elif kind == "StaticDataReport":
        part_a, part_b = body.get("ReportA") or {}, body.get("ReportB") or {}
        if part_a.get("Valid"):
            _static(vessel, {"Name": part_a.get("Name")})
        if part_b.get("Valid"):
            _static(vessel, part_b)


class ShipsCollector(Collector):
    name = "ships"
    uses_location = True
    enabled_by_default = False
    required_env = ("AISSTREAM_API_KEY",)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.api_key = os.environ["AISSTREAM_API_KEY"]
        self.interval = max(MIN_INTERVAL_SECONDS, float(self.module_config.get("poll_seconds", 15)))
        self.radius_km = max(1.0, float(self.module_config.get("radius_km", 50)))
        self.stale_after = timedelta(minutes=float(self.module_config.get("stale_minutes", 15)))
        self._started = _now()

    def _subscription(self) -> dict:
        lat, lon, _ = self.home()
        dlat = self.radius_km / 111.32
        dlon = self.radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
        return {
            "APIKey": self.api_key,
            "BoundingBoxes": [[[lat - dlat, lon - dlon], [lat + dlat, lon + dlon]]],
            "FilterMessageTypes": MESSAGE_TYPES,
        }

    async def start(self, bus: Bus) -> None:
        stream = asyncio.create_task(self._stream(), name="ships-stream")
        try:
            await super().start(bus)
        finally:
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)

    async def _stream(self) -> None:
        backoff = STREAM_BACKOFF[0]
        while True:
            delay = None
            try:
                async with websockets.connect(
                    STREAM_URL, compression="deflate", open_timeout=10, max_size=2**20
                ) as ws:
                    await ws.send(json.dumps(self._subscription()))
                    # An open socket that has stopped delivering is as dead as a
                    # closed one: reconnect after STREAM_SILENT without a frame.
                    silent = STREAM_SILENT.total_seconds()
                    loop = asyncio.get_running_loop()
                    async with asyncio.timeout(silent) as deadline:
                        async for frame in ws:
                            deadline.reschedule(loop.time() + silent)
                            before = _stream["last_message"]
                            ingest(json.loads(frame), _now())
                            if _stream["last_message"] is not before:
                                backoff = STREAM_BACKOFF[0]
                                _stream["error"] = None
                    reason = f" ({ws.close_reason})" if ws.close_reason else ""
                    _stream["error"] = f"stream closed by aisstream: {ws.close_code}{reason}"
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _stream["error"] = f"no frames for {STREAM_SILENT.seconds // 60} min — reconnecting"
            except StreamRejected as exc:
                _stream["error"] = f"aisstream refused the subscription: {exc}"
                delay = REJECTED_BACKOFF
            except Exception as exc:  # noqa: BLE001 — reconnect; fetch() reports it
                _stream["error"] = f"{type(exc).__name__}: {exc}"
            log.debug("ships stream: %s", _stream["error"])
            await _sleep((delay or backoff) * random.uniform(1.0, 1.25))
            backoff = min(backoff * 2, STREAM_BACKOFF[1])

    async def fetch(self) -> dict:
        now = _now()
        last = _stream["last_message"]
        if last is None or now - last > STREAM_SILENT:
            since = last or self._started
            if now - since > STREAM_SILENT or (last is None and _stream["error"]):
                raise RuntimeError(_stream["error"] or "no AIS messages from aisstream")
        problems = []
        if _stream["compression"] is False:
            problems.append("AIS stream uncompressed — aisstream caps its bandwidth")
        if _stream["error"]:  # still within STREAM_SILENT of the last message
            problems.append(f"AIS stream reconnecting: {_stream['error']}")
        self.degraded = "; ".join(problems) or None
        listening = _stream["listening_since"]
        return {
            "vessels": [dict(v) for v in _vessels.values()],
            "warming": listening is None or now - listening < WARMUP,
        }

    def shape(self, raw: dict) -> ModulePayload:
        now = _now()
        lat0, lon0, place = self.home()
        cutoff = now - self.stale_after
        vessels = []
        for v in raw.get("vessels") or []:
            if v.get("heard", now) < cutoff:
                _vessels.pop(v["mmsi"], None)  # gone quiet: out of range or switched off
                continue
            if v.get("lat") is None or v.get("seen") is None or v["seen"] < cutoff:
                continue
            distance = haversine_km(lat0, lon0, v["lat"], v["lon"])
            if distance > self.radius_km:
                continue
            bearing = bearing_deg(lat0, lon0, v["lat"], v["lon"])
            kind, kind_label = kind_of(v.get("type"))
            status = v.get("status")
            sog = v.get("sog")
            moving = status not in STOPPED_STATUSES and sog is not None and sog >= MOVING_KNOTS
            flag = flag_of(v["mmsi"])
            name = v.get("name") or v.get("meta_name") or v.get("callsign") or f"MMSI {v['mmsi']}"
            vessels.append({
                "mmsi": str(v["mmsi"]),
                "name": name,
                "callsign": v.get("callsign"),
                "imo": v.get("imo"),
                "kind": kind,
                "kind_label": kind_label,
                "flag": flag[0] if flag else None,
                "country": flag[1] if flag else None,
                "lat": v["lat"],
                "lon": v["lon"],
                "speed_kn": round(sog, 1) if sog is not None else None,
                "course": round(v["cog"]) if v.get("cog") is not None else None,
                "heading": round(v["heading"]) if v.get("heading") is not None else None,
                "status": NAV_STATUS.get(status) if isinstance(status, int) else None,
                "moving": moving,
                "distance_km": round(distance, 1),
                "bearing_deg": round(bearing, 1),
                "direction": compass_octant(bearing),
                "length_m": v.get("length_m"),
                "beam_m": v.get("beam_m"),
                "draught_m": v.get("draught_m"),
                "destination": v.get("destination"),
                "eta": eta_of(v.get("eta_raw"), now),
                "ais_class": v.get("ais_class"),
                "age_s": round((now - v["seen"]).total_seconds()),
            })
        # Under way first, then nearest: the marinas otherwise fill the list
        # with moored boats (the ADS-B page's airborne-first rule).
        vessels.sort(key=lambda s: (not s["moving"], s["distance_km"]))
        moving = [s for s in vessels if s["moving"]]
        underway = [s for s in moving if s["speed_kn"] >= TAPE_KNOTS]
        notable = [s for s in underway if s["kind"] in TAPE_KINDS]
        tape = [
            TapeItem(
                text=" · ".join(
                    [s["name"]]
                    + ([s["kind_label"]] if s["kind"] not in ("unknown", "other") else [])
                    + [f"{s['speed_kn']:.0f} kn" + (f" → {s['destination']}" if s["destination"] else "")]
                    + [f"{s['distance_km']:.0f} km {s['direction']}"]
                ),
                icon="ship",
            )
            for s in (notable or underway)[:3]
        ]
        return ModulePayload(
            module=self.name,
            stage={
                "vessels": vessels[:MAX_VESSELS],
                "count_in_radius": len(vessels),
                "count_moving": len(moving),
                "radius_km": self.radius_km,
                "center": {"lat": lat0, "lon": lon0},
                "location_name": place,
                "warming": bool(raw.get("warming")),
            },
            tape=tape,
        )
