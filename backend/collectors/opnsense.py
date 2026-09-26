"""OPNsense network collector (stretch) — which WAN the house is on, gateway
health, WAN and VLAN throughput, firewall vitals.

Needs OPNSENSE_URL + OPNSENSE_KEY + OPNSENSE_SECRET in .env (an API key pair,
sent as HTTP basic auth); skipped automatically when they're absent. The GUI's
certificate is self-signed, so TLS verification is off unless
OPNSENSE_VERIFY_SSL=1.

Which WAN is active: the interface of the IPv4 default route while its gateway
is online — that is what OPNsense's default-gateway switching moves. When the
default route points at a dead gateway (gateway-group policy routing leaves it
in place) the online WAN gateway with the lowest priority number is the one
carrying traffic. No online gateway at all means the house is offline.

Throughput comes from the interface byte counters, differenced between polls
against the firewall's own clock. A VLAN parent's counters include every tagged
VLAN riding it (ix1 "LAN" ≈ the sum of its VLANs), so parents report only
what is left after their children — otherwise LAN double-counts the house.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

# Any failure here fails the poll: without them there is no WAN picture.
CORE_ENDPOINTS = {
    "gateway_status": "routes/gateway/status",
    "gateways": "routing/settings/searchGateway",
    "routes": "diagnostics/interface/getRoutes",
    "traffic": "diagnostics/traffic/interface",
    "interfaces": "interfaces/overview/interfacesInfo",
}
# Best effort: a failure keeps the last good value and marks the module degraded.
HEALTH_ENDPOINTS = {
    "system_time": "diagnostics/system/systemTime",
    "resources": "diagnostics/system/systemResources",
    "states": "diagnostics/firewall/pf_states",
}
VERSION_ENDPOINT = "diagnostics/system/systemInformation"
VERSION_REFRESH_SECONDS = 3600.0
SPARK_POINTS = 48
# Tunnels and pseudo-interfaces: not networks anyone lives on.
NOT_A_NETWORK = re.compile(r"^(lo|enc|ipsec|wg|wireguard|pflog|pfsync|ovpn|tun|gif|gre)")
# dpinger status codes: "none" is healthy; these are up-but-suffering.
WARN_STATUSES = {"loss", "delay", "delay+loss"}
DOWN_STATUSES = {"down", "force_down"}

# Module-level on purpose (the weather_alerts._fired_ids precedent): a config
# save restarts the collector, and instance state would blank the rates, drop
# the sparklines and forget when the failover began.
_counters: dict[str, tuple[float, int, int]] = {}  # device -> (fw time, rx, tx)
_history: dict[str, deque] = {}  # "<wan id>:<series>" -> deque[(fw time, value)]
_active: dict = {"seen": False, "id": None, "since": None}


def _num(value: object) -> float | None:
    """'8.2 ms' / '0.0 %' / '~' / 42 -> float or None."""
    if isinstance(value, (int, float)):
        return float(value)
    match = re.match(r"\s*(-?[\d.]+)", str(value or ""))
    return float(match.group(1)) if match else None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def fmt_rate(bps: float | None) -> str:
    if bps is None:
        return "—"
    if bps >= 1e9:
        return f"{bps / 1e9:.2f} Gb/s"
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} Mb/s" if bps < 1e8 else f"{bps / 1e6:.0f} Mb/s"
    return f"{bps / 1e3:.0f} kb/s"


def _uptime(text: str | None) -> str | None:
    """systemTime's '20:30:54' or '3 days, 04:12:08' -> '20h 30m' / '3d 4h'."""
    if not text:
        return None
    days = re.search(r"(\d+)\s*day", text)
    clock = re.search(r"(\d+):(\d+)(?::\d+)?\s*$", text)
    if not clock:
        return text
    d = int(days.group(1)) if days else 0
    h, m = int(clock.group(1)), int(clock.group(2))
    return f"{d}d {h}h" if d else f"{h}h {m}m"


def _downsample(points: deque) -> list[float]:
    """Bucket averages, not point samples: a throughput burst between samples
    is exactly what the sparkline is for."""
    values = [v for _, v in points]
    if len(values) <= SPARK_POINTS:
        return [round(v, 2) for v in values]
    size = len(values) / SPARK_POINTS
    out = []
    for i in range(SPARK_POINTS):
        bucket = values[round(i * size) : round((i + 1) * size)] or [values[-1]]
        out.append(round(sum(bucket) / len(bucket), 2))
    return out


class OpnsenseCollector(Collector):
    name = "opnsense"
    enabled_by_default = False
    required_env = ("OPNSENSE_URL", "OPNSENSE_KEY", "OPNSENSE_SECRET")

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 15))
        self.base_url = os.environ["OPNSENSE_URL"].rstrip("/")
        self.auth = httpx.BasicAuth(os.environ["OPNSENSE_KEY"], os.environ["OPNSENSE_SECRET"])
        self.verify_ssl = os.environ.get("OPNSENSE_VERIFY_SSL", "") == "1"
        self.primary = str(self.module_config.get("primary_interface") or "wan")
        self.warn_backup_down = self.module_config.get("warn_backup_down", True) is not False
        self.history_seconds = 60.0 * float(self.module_config.get("history_minutes", 60))
        self.vlan_exclude = {
            str(n).lower() for n in (self.module_config.get("vlan_exclude") or [])
        }
        self._health: dict[str, dict] = {}  # last good response per health endpoint
        self._version: str | None = None
        self._version_at = 0.0

    # ---- fetch ---------------------------------------------------------
    async def _get(self, client: httpx.AsyncClient, endpoint: str):
        response = await client.get(f"{self.base_url}/api/{endpoint}")
        response.raise_for_status()
        return response.json()

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=10, auth=self.auth, verify=self.verify_ssl) as client:
            health = dict(HEALTH_ENDPOINTS)
            if time.monotonic() - self._version_at > VERSION_REFRESH_SECONDS or not self._version:
                health["version"] = VERSION_ENDPOINT
            jobs = {**CORE_ENDPOINTS, **health}
            # return_exceptions: a failed core call must not leave the others
            # running against a client that is about to close.
            results = dict(
                zip(jobs, await asyncio.gather(
                    *(self._get(client, ep) for ep in jobs.values()), return_exceptions=True
                ))
            )
        for key in CORE_ENDPOINTS:
            if isinstance(results[key], BaseException):
                raise results[key]
        failed = []
        for key in health:
            result = results[key]
            if isinstance(result, BaseException):
                failed.append(key)
                log.debug("opnsense %s failed: %s", key, result)
            elif key == "version":
                versions = result.get("versions") or []
                match = re.match(r"OPNsense\s+(\S+?)(?:-\w+)?$", str(versions[0])) if versions else None
                self._version = match.group(1) if match else self._version
                self._version_at = time.monotonic()
            else:
                self._health[key] = result
        self.degraded = f"firewall stats unavailable: {', '.join(failed)}" if failed else None
        raw = {key: results[key] for key in CORE_ENDPOINTS}
        raw["health"] = dict(self._health)
        raw["version"] = self._version
        return raw

    # ---- shape ---------------------------------------------------------
    def _rates(self, traffic: dict) -> dict[str, tuple[float | None, float | None]]:
        """device -> (rx bps, tx bps) since the previous poll; None on the
        first poll, a clock that didn't advance, or a counter reset."""
        now = float(traffic.get("time") or time.time())
        rates: dict[str, tuple[float | None, float | None]] = {}
        for entry in (traffic.get("interfaces") or {}).values():
            device = entry.get("device")
            rx, tx = _int(entry.get("bytes received")), _int(entry.get("bytes transmitted"))
            if not device or rx is None or tx is None:
                continue
            previous = _counters.get(device)
            _counters[device] = (now, rx, tx)
            rx_bps = tx_bps = None
            if previous and now > previous[0]:
                dt = now - previous[0]
                if rx >= previous[1]:
                    rx_bps = (rx - previous[1]) * 8 / dt
                if tx >= previous[2]:
                    tx_bps = (tx - previous[2]) * 8 / dt
            rates[device] = (rx_bps, tx_bps)
        return rates

    def _record(self, key: str, now: float, value: float | None) -> list[float]:
        points = _history.setdefault(key, deque())
        if value is not None:
            points.append((now, value))
        while points and points[0][0] < now - self.history_seconds:
            points.popleft()
        return _downsample(points)

    def shape(self, raw: dict) -> ModulePayload:
        traffic = raw.get("traffic") or {}
        now = float(traffic.get("time") or time.time())
        rates = self._rates(traffic)
        info = {r.get("device"): r for r in (raw.get("interfaces") or {}).get("rows", [])}
        traffic_by_id = traffic.get("interfaces") or {}
        status_by_name = {
            g.get("name"): g for g in (raw.get("gateway_status") or {}).get("items", [])
        }

        # One card per WAN interface: its best IPv4 gateway.
        gateways: dict[str, dict] = {}
        for gw in (raw.get("gateways") or {}).get("rows", []):
            if gw.get("ipprotocol") != "inet" or gw.get("disabled") in (True, "1"):
                continue
            iface = gw.get("interface")
            priority = _int(gw.get("priority"))
            gw["_priority"] = 255 if priority is None else priority
            if iface and (iface not in gateways or gw["_priority"] < gateways[iface]["_priority"]):
                gateways[iface] = gw

        wans = []
        for iface, gw in gateways.items():
            device = gw.get("if") or (traffic_by_id.get(iface) or {}).get("device")
            row = info.get(device) or {}
            live = status_by_name.get(gw.get("name")) or {}
            code = str(live.get("status") or gw.get("status") or "").lower()
            link = row.get("status") or "unknown"
            if link != "up":
                health = "nolink"
            elif code in DOWN_STATUSES or str(live.get("status_translated")).lower() == "offline":
                health = "down"
            elif code in WARN_STATUSES:
                health = "warn"
            else:
                health = "up"
            name = gw.get("interface_descr") or row.get("description") or iface.upper()
            rx_bps, tx_bps = rates.get(device, (None, None))
            # A port without carrier still reports its autoselect default.
            line = _num((traffic_by_id.get(iface) or {}).get("line rate")) if link == "up" else None
            address = live.get("address") if live.get("address") not in (None, "~") else None
            wans.append({
                "id": iface,
                "name": name,
                "kind": "satellite" if "starlink" in f"{name} {gw.get('name')}".lower() else "wired",
                "device": device,
                "gateway": gw.get("name"),
                "gateway_ip": address,
                "monitor": live.get("monitor") if live.get("monitor") not in (None, "~") else None,
                "health": health,
                "status_text": live.get("status_translated") or gw.get("status"),
                "link": link,
                "media": row.get("media"),
                "ip": (row.get("addr4") or "").split("/")[0] or None,
                "latency_ms": _num(live.get("delay")),
                "loss_pct": _num(live.get("loss")),
                "jitter_ms": _num(live.get("stddev")),
                "down_bps": rx_bps,
                "up_bps": tx_bps,
                "line_bps": line,
                "priority": gw["_priority"],
                "primary": iface == self.primary,
                "active": False,
            })
        # Primary first, then by failover priority.
        wans.sort(key=lambda w: (not w["primary"], w["priority"], w["name"]))

        # ---- which WAN is carrying traffic
        online = [w for w in wans if w["health"] in ("up", "warn")]
        default = next(
            (r for r in (raw.get("routes") or [])
             if r.get("proto") == "ipv4" and r.get("destination") == "default"),
            None,
        )
        routed = next((w for w in online if default and w["device"] == default.get("netif")), None)
        active = routed or min(
            online, key=lambda w: (w["priority"], not w["primary"]), default=None
        )
        if active:
            active["active"] = True
        active_id = active["id"] if active else None
        if not _active["seen"]:
            _active.update(seen=True, id=active_id, since=None)
        elif _active["id"] != active_id:
            _active.update(
                id=active_id, since=datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
        failover = bool(active and not active["primary"])
        offline = active is None

        for wan in wans:
            wan["history"] = {
                "latency": self._record(f"{wan['id']}:latency", now, wan["latency_ms"]),
                "down": self._record(f"{wan['id']}:down", now, wan["down_bps"]),
                "up": self._record(f"{wan['id']}:up", now, wan["up_bps"]),
            }

        vlans = self._vlans(traffic_by_id, info, rates, {w["device"] for w in wans})
        system = self._system(raw.get("health") or {}, raw.get("version"))

        stage = {
            "active": (
                {
                    "id": active["id"],
                    "name": active["name"],
                    "kind": active["kind"],
                    "ip": active["ip"],
                    "since": _active["since"],
                }
                if active
                else None
            ),
            "failover": failover,
            "offline": offline,
            "primary": self.primary,
            "wans": wans,
            "vlans": vlans,
            "system": system,
            "history_minutes": round(self.history_seconds / 60),
        }
        return ModulePayload(module=self.name, stage=stage, tape=self._tape(wans, active))

    def _vlans(
        self,
        traffic_by_id: dict,
        info: dict,
        rates: dict[str, tuple[float | None, float | None]],
        wan_devices: set,
    ) -> list[dict]:
        children: dict[str, list[str]] = {}
        for device, row in info.items():
            parent = (row.get("vlan") or {}).get("parent")
            if parent:
                children.setdefault(parent, []).append(device)
        out = []
        for entry in traffic_by_id.values():
            device, name = entry.get("device") or "", entry.get("name") or ""
            if (
                device in wan_devices
                or NOT_A_NETWORK.match(device)
                or name.lower() in self.vlan_exclude
            ):
                continue
            rx_bps, tx_bps = rates.get(device, (None, None))
            for child in children.get(device, []):
                child_rx, child_tx = rates.get(child, (None, None))
                rx_bps = None if rx_bps is None or child_rx is None else max(0.0, rx_bps - child_rx)
                tx_bps = None if tx_bps is None or child_tx is None else max(0.0, tx_bps - child_tx)
            # From the network's side: what the firewall sends into it is its
            # download.
            out.append({"name": name, "down_bps": tx_bps, "up_bps": rx_bps})
        out.sort(key=lambda v: (-((v["down_bps"] or 0) + (v["up_bps"] or 0)), v["name"]))
        return out

    @staticmethod
    def _system(health: dict, version: str | None) -> dict | None:
        clock = health.get("system_time") or {}
        memory = (health.get("resources") or {}).get("memory") or {}
        states = health.get("states") or {}
        total, used = _num(memory.get("total")), _num(memory.get("used"))
        system = {
            "version": version,
            "uptime": _uptime(clock.get("uptime")),
            "load": _num(clock.get("loadavg")),
            "mem_pct": round(100 * used / total, 1) if total and used is not None else None,
            "states": _int(states.get("current")),
            "states_limit": _int(states.get("limit")),
        }
        return system if any(v is not None for v in system.values()) else None

    def _tape(self, wans: list[dict], active: dict | None) -> list[TapeItem]:
        tape = []
        primary = next((w for w in wans if w["primary"]), None)
        if active is None:
            tape.append(TapeItem(
                text="INTERNET DOWN · no WAN gateway online", accent="alert", priority=5,
                icon="warning",
            ))
        else:
            if not active["primary"]:
                why = (
                    f"{primary['name']} {self._down_reason(primary)}"
                    if primary and primary["health"] not in ("up", "warn")
                    else f"{primary['name']} bypassed" if primary else "primary WAN missing"
                )
                tape.append(TapeItem(
                    text=f"FAILOVER · on {active['name']} · {why}", accent="alert", priority=5,
                    icon=active["kind"],
                ))
            parts = []
            if active["latency_ms"] is not None:
                parts.append(f"{active['latency_ms']:.0f} ms")
            if active["loss_pct"] is not None:
                parts.append(f"{active['loss_pct']:.0f}% loss")
            if active["down_bps"] is not None:
                parts.append(f"▼ {fmt_rate(active['down_bps'])} ▲ {fmt_rate(active['up_bps'])}")
            head = f"{active['name']} {parts.pop(0)}" if parts else active["name"]
            tape.append(TapeItem(
                text=" · ".join([head, *parts]),
                accent="down" if active["health"] == "warn" else "neutral",
                priority=1,
                icon=active["kind"],
            ))
        if self.warn_backup_down and active is not None:
            for wan in wans:
                if wan["active"] or wan["primary"] or wan["health"] in ("up", "warn"):
                    continue
                tape.append(TapeItem(
                    text=f"{wan['name']} backup offline · {self._down_reason(wan)}",
                    accent="down",
                    priority=0,
                    icon=wan["kind"],
                ))
        return tape

    @staticmethod
    def _down_reason(wan: dict) -> str:
        return wan["link"] if wan["health"] == "nolink" else "gateway down"
