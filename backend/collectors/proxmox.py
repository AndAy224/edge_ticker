"""Proxmox stats collector (stretch) — node CPU/memory, guest counts, storage.

Needs PVE_URL + PVE_TOKEN_ID + PVE_TOKEN_SECRET in .env (API token auth);
skipped automatically when they're absent. Self-signed certs are the norm on
PVE, so TLS verification is off unless PVE_VERIFY_SSL=1.

Optionally also meters the node's PSU feeds off a UniFi smart PDU
(UNIFI_URL + UNIFI_API_KEY, plus a modules.proxmox.pdu config block). That is
strictly additive: no UniFi env, no PDU config, or a UniFi controller that is
down all leave the rest of the module untouched.
"""
from __future__ import annotations

import logging
import os

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

# Below this a PSU is coasting, not carrying — a dual-feed server showing
# 0.4 W on one side has lost that side, it hasn't got a light load.
FEED_LIVE_WATTS = 5.0


def _num(value: object) -> float | None:
    """UniFi reports outlet metering as decimal *strings* ("177.063")."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class ProxmoxCollector(Collector):
    name = "proxmox"
    enabled_by_default = False
    required_env = ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET")

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.interval = float(self.module_config.get("poll_seconds", 60))
        self.base_url = os.environ["PVE_URL"].rstrip("/")
        token_id = os.environ["PVE_TOKEN_ID"]
        token_secret = os.environ["PVE_TOKEN_SECRET"]
        self.headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        self.verify_ssl = os.environ.get("PVE_VERIFY_SSL", "") == "1"

        # --- optional PDU metering -------------------------------------
        # Deliberately NOT in required_env: the UniFi side is a bonus, and
        # gating the whole PVE module on it would be a regression for anyone
        # without a smart PDU. Existing config DBs never gain new default
        # keys, so every lookup here has to stand on its own default.
        pdu = self.module_config.get("pdu") or {}
        self.pdu_url = (os.environ.get("UNIFI_URL") or "").rstrip("/")
        self.pdu_key = os.environ.get("UNIFI_API_KEY") or ""
        self.pdu_site = pdu.get("site") or "default"
        self.pdu_mac = (pdu.get("device_mac") or "").lower()
        self.pdu_node = pdu.get("node") or ""
        self.pdu_verify_ssl = os.environ.get("UNIFI_VERIFY_SSL", "") == "1"
        feeds = pdu.get("feeds") or []
        self.pdu_feeds = [f for f in feeds if isinstance(f, dict) and f.get("outlet")]
        self.pdu_enabled = bool(
            pdu.get("enabled", True)
            and self.pdu_url
            and self.pdu_key
            and self.pdu_mac
            and self.pdu_feeds
        )
        self._pdu_cache: dict | None = None

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(
            timeout=15, headers=self.headers, verify=self.verify_ssl
        ) as client:
            response = await client.get(f"{self.base_url}/api2/json/cluster/resources")
        response.raise_for_status()
        return {"resources": response.json()["data"], "pdu": await self._fetch_pdu()}

    async def _fetch_pdu(self) -> dict | None:
        """Outlet metering from the UniFi controller — never fatal.

        The controller is a far less reliable dependency than PVE itself (it
        may even be a guest *on* the node being metered, in which case a host
        reboot takes it down too). A failure here must not mark the whole
        module stale, so it falls back to the last good reading instead.

        Outlet-level power only exists on the legacy `/api/s/<site>/stat/device`
        route; the modern `/integration/v1` device endpoints stop at CPU and
        memory. Both accept the same X-API-KEY header.
        """
        if not self.pdu_enabled:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=10,
                headers={"X-API-KEY": self.pdu_key, "Accept": "application/json"},
                verify=self.pdu_verify_ssl,
            ) as client:
                response = await client.get(
                    f"{self.pdu_url}/proxy/network/api/s/{self.pdu_site}"
                    f"/stat/device/{self.pdu_mac}"
                )
            response.raise_for_status()
            devices = response.json().get("data") or []
            if not devices:
                raise ValueError(f"PDU {self.pdu_mac} not found on site {self.pdu_site}")
            device = devices[0]
        except Exception as exc:
            log.debug("proxmox: PDU poll failed (%s: %s)", type(exc).__name__, exc)
            if self._pdu_cache is None:
                return None
            return {**self._pdu_cache, "stale": True}

        outlets = {}
        for outlet in device.get("outlet_table") or []:
            idx = outlet.get("index")
            if idx is None:
                continue
            outlets[int(idx)] = {
                # USB outlets are switchable but unmetered — watts stays None.
                "watts": _num(outlet.get("outlet_power")),
                "amps": _num(outlet.get("outlet_current")),
                "volts": _num(outlet.get("outlet_voltage")),
                "relay_on": bool(outlet.get("relay_state")),
            }
        self._pdu_cache = {
            "outlets": outlets,
            "site_w": _num(device.get("outlet_ac_power_consumption")),
            "budget_w": _num(device.get("outlet_ac_power_budget")),
            "stale": False,
        }
        return self._pdu_cache

    def _shape_power(self, pdu: dict | None, nodes: list[dict]) -> dict | None:
        if not pdu:
            return None
        readings = pdu.get("outlets") or {}
        feeds = []
        for feed in self.pdu_feeds:
            idx = int(feed["outlet"])
            reading = readings.get(idx)
            if reading is None:
                continue
            watts = reading["watts"]
            feeds.append(
                {
                    "outlet": idx,
                    "label": feed.get("label") or f"Outlet {idx}",
                    "watts": watts,
                    "amps": reading["amps"],
                    "volts": reading["volts"],
                    "relay_on": reading["relay_on"],
                    "live": reading["relay_on"] and (watts or 0) >= FEED_LIVE_WATTS,
                }
            )
        if not feeds:
            return None
        live = [f for f in feeds if f["live"]]
        if not live:
            state = "off"
        elif len(live) == len(feeds):
            state = "ok"
        else:
            state = "degraded"
        return {
            # Which node card this belongs to. A lone node needs no config;
            # a cluster has to say, or the draw would land on an arbitrary host.
            "node": self.pdu_node or (nodes[0]["name"] if len(nodes) == 1 else None),
            "feeds": feeds,
            "total_w": round(sum(f["watts"] or 0 for f in feeds), 1),
            "state": state,
            "site_w": pdu.get("site_w"),
            "budget_w": pdu.get("budget_w"),
            "stale": bool(pdu.get("stale")),
        }

    def shape(self, raw: dict) -> ModulePayload:
        resources: list[dict] = raw["resources"]
        nodes = []
        guests_running = 0
        guests_total = 0
        guests: list[dict] = []
        storage = []
        for r in resources:
            kind = r.get("type")
            if kind == "node":
                nodes.append(
                    {
                        "name": r.get("node"),
                        "online": r.get("status") == "online",
                        "cpu": round((r.get("cpu") or 0) * 100, 1),
                        "mem_pct": round((r.get("mem") or 0) / (r.get("maxmem") or 1) * 100, 1),
                        "mem_used_gb": round((r.get("mem") or 0) / 2**30, 1),
                        "mem_total_gb": round((r.get("maxmem") or 0) / 2**30, 1),
                        "uptime": r.get("uptime") or 0,
                    }
                )
            elif kind in ("qemu", "lxc"):
                guests_total += 1
                running = r.get("status") == "running"
                if running:
                    guests_running += 1
                # The same poll already carries every guest; the stage leads
                # with the busiest ones rather than just a count.
                guests.append(
                    {
                        "vmid": r.get("vmid"),
                        "name": r.get("name") or f"{kind}-{r.get('vmid')}",
                        "type": kind,
                        "running": running,
                        "cpu": round((r.get("cpu") or 0) * 100, 1),
                        "mem_gb": round((r.get("mem") or 0) / 2**30, 1),
                        "maxmem_gb": round((r.get("maxmem") or 0) / 2**30, 1),
                    }
                )
            elif kind == "storage" and r.get("maxdisk"):
                used = r.get("disk") or 0
                storage.append(
                    {
                        "name": r.get("storage"),
                        "node": r.get("node"),
                        "pct": round(used / r["maxdisk"] * 100, 1),
                        "used_gb": round(used / 2**30, 1),
                        "total_gb": round(r["maxdisk"] / 2**30, 1),
                        "free_gb": round((r["maxdisk"] - used) / 2**30, 1),
                    }
                )
        storage.sort(key=lambda s: -s["pct"])
        nodes.sort(key=lambda n: n["name"] or "")
        # busiest first; stopped guests are represented by the count alone
        guests.sort(key=lambda g: (not g["running"], -g["cpu"]))
        busiest = [g for g in guests if g["running"]][:10]
        power = self._shape_power(raw.get("pdu"), nodes)

        tape = []
        for node in nodes:
            hot = node["cpu"] >= 90 or node["mem_pct"] >= 90 or not node["online"]
            text = (
                f"PVE {node['name']}: CPU {node['cpu']:.0f}% · MEM {node['mem_pct']:.0f}%"
                if node["online"]
                else f"PVE {node['name']}: OFFLINE"
            )
            if power and power["node"] == node["name"] and node["online"]:
                text += f" · {power['total_w']:.0f} W"
            tape.append(TapeItem(text=text, accent="alert" if hot else "neutral"))
        if power and power["state"] == "degraded":
            dead = ", ".join(f["label"] for f in power["feeds"] if not f["live"])
            tape.append(
                TapeItem(
                    text=f"PVE {power['node'] or 'node'}: {dead} not drawing — no PSU redundancy",
                    accent="alert",
                )
            )
        return ModulePayload(
            module=self.name,
            stage={
                "nodes": nodes,
                "guests": {
                    "running": guests_running,
                    "total": guests_total,
                    "busiest": busiest,
                },
                "storage": storage[:6],
                "power": power,
            },
            tape=tape,
        )
