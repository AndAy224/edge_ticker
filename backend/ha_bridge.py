"""Persistent Home Assistant WebSocket client with reconnect/backoff.

Subscribes to state_changed and pushes diffs for mapped entities to the bus.
Service calls (tile taps) are funneled through the same connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Callable

import websockets

log = logging.getLogger(__name__)

ATTRIBUTE_WHITELIST = {
    "friendly_name",
    "device_class",
    "brightness",
    "color_temp",
    "rgb_color",
    "current_temperature",
    "temperature",
    "hvac_action",
    "hvac_modes",
    "media_title",
    "media_artist",
    "volume_level",
    "unit_of_measurement",
}


class HABridge:
    def __init__(self, bus, get_config: Callable[[], dict]) -> None:
        self.bus = bus
        self.get_config = get_config
        self.url = os.environ.get("HA_URL", "").rstrip("/")
        self.token = os.environ.get("HA_TOKEN", "")
        self.status = "disconnected" if self.url and self.token else "unconfigured"
        self.all_states: dict[str, dict] = {}
        self._ws = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    # -- public surface -----------------------------------------------------

    def mapped_states(self) -> dict[str, dict]:
        mapped = self._mapped_ids()
        return {eid: s for eid, s in self.all_states.items() if eid in mapped}

    def list_entities(self) -> list[dict]:
        return sorted(
            (
                {
                    "entity_id": eid,
                    "domain": eid.split(".", 1)[0],
                    "name": s.get("attributes", {}).get("friendly_name", eid),
                    "state": s.get("state"),
                    "device_class": s.get("attributes", {}).get("device_class"),
                }
                for eid, s in self.all_states.items()
            ),
            key=lambda e: (e["domain"], str(e["name"]).lower()),
        )

    async def call_service(
        self,
        domain: str | None,
        service: str | None,
        entity_id: str | None = None,
        data: dict | None = None,
    ):
        if not domain or not service:
            raise ValueError("domain and service are required")
        payload: dict = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": dict(data or {}),
        }
        if entity_id:
            payload["target"] = {"entity_id": entity_id}
        return await self._command(payload)

    # -- connection loop ----------------------------------------------------

    async def run(self) -> None:
        if self.status == "unconfigured":
            log.info("HA bridge disabled — set HA_URL and HA_TOKEN in .env to enable")
            return
        ws_url = (
            self.url.replace("http://", "ws://").replace("https://", "wss://")
            + "/api/websocket"
        )
        backoff = 1.0
        while True:
            try:
                await self._session(ws_url)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("HA bridge: %s", exc)
            await self._set_status("disconnected")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _session(self, ws_url: str) -> None:
        async with websockets.connect(ws_url) as ws:
            self._ws = ws
            try:
                first = json.loads(await ws.recv())
                if first.get("type") == "auth_required":
                    await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                    reply = json.loads(await ws.recv())
                    if reply.get("type") != "auth_ok":
                        raise RuntimeError("HA auth failed — check HA_TOKEN")
                await self._set_status("connected")

                reader = asyncio.create_task(self._reader(ws))
                try:
                    states = await self._command({"type": "get_states"})
                    self.all_states = {s["entity_id"]: self._trim(s) for s in states}
                    await self._command(
                        {"type": "subscribe_events", "event_type": "state_changed"}
                    )
                    await self.bus.broadcast(
                        {
                            "type": "ha_states",
                            "status": self.status,
                            "states": self.mapped_states(),
                        }
                    )
                    await reader  # runs until the connection drops
                finally:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
            finally:
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("HA connection closed"))
                self._pending.clear()

    async def _reader(self, ws) -> None:
        async for raw in ws:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "result":
                fut = self._pending.pop(message.get("id"), None)
                if fut is not None and not fut.done():
                    if message.get("success"):
                        fut.set_result(message.get("result"))
                    else:
                        fut.set_exception(RuntimeError(str(message.get("error"))))
            elif kind == "event":
                await self._on_event(message.get("event", {}))

    async def _command(self, payload: dict):
        ws = self._ws
        if ws is None:
            raise RuntimeError("Home Assistant is not connected")
        self._next_id += 1
        cmd_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        await ws.send(json.dumps({**payload, "id": cmd_id}))
        return await asyncio.wait_for(fut, timeout=10)

    # -- events ---------------------------------------------------------------

    async def _on_event(self, event: dict) -> None:
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data", {})
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not entity_id or new_state is None:
            return
        trimmed = self._trim(new_state)
        self.all_states[entity_id] = trimmed
        if entity_id in self._mapped_ids():
            await self.bus.broadcast(
                {"type": "ha_state", "entity_id": entity_id, **trimmed}
            )

    async def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            await self.bus.broadcast({"type": "ha_status", "status": status})

    def _mapped_ids(self) -> set[str]:
        ha = (self.get_config() or {}).get("ha") or {}
        ids = set(ha.get("scenes") or []) | set(ha.get("lights") or [])
        for key in ("climate", "media"):
            if ha.get(key):
                ids.add(ha[key])
        # Alert entities ride the same state broadcasts (tape alert items).
        for alert in ha.get("alerts") or []:
            if isinstance(alert, dict) and alert.get("entity"):
                ids.add(alert["entity"])
        return ids

    @staticmethod
    def _trim(state: dict) -> dict:
        attrs = state.get("attributes") or {}
        return {
            "state": state.get("state"),
            "attributes": {k: v for k, v in attrs.items() if k in ATTRIBUTE_WHITELIST},
        }
