"""Dev smoke test: exercises REST endpoints and the display WebSocket.

Usage: start the backend on :8080, then `python scripts/smoke_test.py`.
"""
import asyncio
import json

import httpx
import websockets

BASE = "http://127.0.0.1:8080"


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

    async with websockets.connect("ws://127.0.0.1:8080/ws/display") as ws:
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
