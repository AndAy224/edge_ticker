"""Proxmox stats collector (stretch) — node CPU/memory, guest counts, storage.

Needs PVE_URL + PVE_TOKEN_ID + PVE_TOKEN_SECRET in .env (API token auth);
skipped automatically when they're absent. Self-signed certs are the norm on
PVE, so TLS verification is off unless PVE_VERIFY_SSL=1.
"""
from __future__ import annotations

import os

import httpx

from ..state import ModulePayload, TapeItem
from .base import Collector


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

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(
            timeout=15, headers=self.headers, verify=self.verify_ssl
        ) as client:
            response = await client.get(f"{self.base_url}/api2/json/cluster/resources")
        response.raise_for_status()
        return response.json()["data"]

    def shape(self, resources: list[dict]) -> ModulePayload:
        nodes = []
        guests_running = 0
        guests_total = 0
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
                if r.get("status") == "running":
                    guests_running += 1
            elif kind == "storage" and r.get("maxdisk"):
                storage.append(
                    {
                        "name": r.get("storage"),
                        "node": r.get("node"),
                        "pct": round((r.get("disk") or 0) / r["maxdisk"] * 100, 1),
                        "used_gb": round((r.get("disk") or 0) / 2**30, 1),
                        "total_gb": round(r["maxdisk"] / 2**30, 1),
                    }
                )
        storage.sort(key=lambda s: -s["pct"])
        nodes.sort(key=lambda n: n["name"] or "")

        tape = []
        for node in nodes:
            hot = node["cpu"] >= 90 or node["mem_pct"] >= 90 or not node["online"]
            text = (
                f"PVE {node['name']}: CPU {node['cpu']:.0f}% · MEM {node['mem_pct']:.0f}%"
                if node["online"]
                else f"PVE {node['name']}: OFFLINE"
            )
            tape.append(TapeItem(text=text, accent="alert" if hot else "neutral"))
        return ModulePayload(
            module=self.name,
            stage={
                "nodes": nodes,
                "guests": {"running": guests_running, "total": guests_total},
                "storage": storage[:6],
            },
            tape=tape,
        )
