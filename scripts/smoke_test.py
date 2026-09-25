"""Dev smoke test: exercises REST endpoints and the display WebSocket.

Usage: start a dev backend (`.venv/bin/uvicorn backend.main:app --port 8081`),
then `.venv/bin/python scripts/smoke_test.py`. Targets TICKER_URL, default
http://127.0.0.1:8081 — the dev port. Pointing it at prod (:8080) is possible
but deliberate: the script PUTs the config back and posts a `next` control.
"""
import asyncio
import json
import os

import httpx
import websockets

BASE = os.environ.get("TICKER_URL", "http://127.0.0.1:8081").rstrip("/")
WS_BASE = "ws" + BASE.removeprefix("http")  # http→ws, https→wss


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE) as client:
        health = (await client.get("/api/health")).json()
        print(
            "HEALTH:",
            json.dumps(
                {c["name"]: (c["stale"], c["last_error"]) for c in health["collectors"]}
            ),
        )
        print("DISPLAY PAGE:", (await client.get("/display")).status_code)
        print("ADMIN PAGE:", (await client.get("/admin")).status_code)
        config = (await client.get("/api/config")).json()
        print("CONFIG rotation:", config["rotation"])
        print("CONTROL:", (await client.post("/api/control", json={"action": "next"})).json())
        print(
            "BAD CONTROL:",
            (await client.post("/api/control", json={"action": "nope"})).status_code,
        )
        print("HA ENTITIES:", (await client.get("/api/ha/entities")).json()["status"])

    async with websockets.connect(f"{WS_BASE}/ws/display") as ws:
        snapshot = json.loads(await ws.recv())
        print(
            "WS SNAPSHOT modules:",
            sorted(snapshot["modules"].keys()),
            "ha:",
            snapshot["ha"]["status"],
        )
        for name, payload in snapshot["modules"].items():
            print(
                f"  {name}: stale={payload['stale']} "
                f"stage_keys={list(payload['stage'].keys())} tape={len(payload['tape'])}"
            )
        await ws.send(json.dumps({"type": "ping"}))
        print("WS PONG:", json.loads(await ws.recv())["type"])

    # Config round-trip: PUT the same document back, expect ok + collectors restart.
    async with httpx.AsyncClient(base_url=BASE) as client:
        config = (await client.get("/api/config")).json()
        print("CONFIG PUT:", (await client.put("/api/config", json=config)).json())
        health = (await client.get("/api/health")).json()
        print("COLLECTORS AFTER RESTART:", [c["name"] for c in health["collectors"]])


if __name__ == "__main__":
    asyncio.run(main())
