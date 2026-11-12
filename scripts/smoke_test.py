"""Dev smoke test: exercises REST endpoints and the display WebSocket.

Runs against a FIXTURE-MODE backend (recorded payloads, nothing polled — a
plain dev backend polls every rate-limited upstream with the dev .env):

    TICKER_FIXTURE=scripts/fixtures/display_snapshot.json \\
      TICKER_DB=/tmp/dev.db FINNHUB_KEY= HA_URL= \\
      .venv/bin/uvicorn backend.main:app --port 8081
    .venv/bin/python scripts/smoke_test.py

Targets TICKER_URL, default http://127.0.0.1:8081 — the dev port. The script
PUTs the config back without If-Match (a concurrent admin save is overwritten,
and collectors restart) and posts a `next` control that moves the panel, so it
refuses a backend that is not in fixture mode — prod on :8080 above all —
unless TICKER_ALLOW_LIVE=1 says that is meant.
"""
import asyncio
import json
import os
import sys

import httpx
import websockets

BASE = os.environ.get("TICKER_URL", "http://127.0.0.1:8081").rstrip("/")
WS_BASE = "ws" + BASE.removeprefix("http")  # http→ws, https→wss


def require_fixture(health: dict) -> None:
    if health.get("fixture") or os.environ.get("TICKER_ALLOW_LIVE") == "1":
        return
    sys.exit(
        f"{BASE} is not a fixture-mode backend — refusing to PUT its config and "
        "post a control (TICKER_ALLOW_LIVE=1 to run against it anyway)"
    )


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE) as client:
        health = (await client.get("/api/health")).json()
        require_fixture(health)
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
