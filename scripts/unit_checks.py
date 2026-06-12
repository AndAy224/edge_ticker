"""Dev checks for pieces the HTTP smoke test can't reach: scheduler broadcast,
display_state relay, stretch collector shaping. Run with the backend up."""
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ADSB_URL", "http://example.invalid/aircraft.json")

from backend.collectors.adsb import AdsbCollector  # noqa: E402
from backend.collectors.astro import AstroCollector, moon_phase  # noqa: E402
from backend.collectors.proxmox import ProxmoxCollector  # noqa: E402
from backend.scheduler import NightScheduler  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


class FakeBus:
    def __init__(self) -> None:
        self.messages = []

    async def broadcast(self, message: dict) -> None:
        self.messages.append(message)


async def check_scheduler() -> None:
    bus = FakeBus()
    scheduler = NightScheduler(
        bus,
        lambda: {
            "night": {
                "dim_at": "23:00",
                "wake_at": "07:00",
                "dim_level": 10,
                "method": "software",
                "nightly_reload_at": "04:00",
            }
        },
    )
    await scheduler._tick("23:00")
    await scheduler._tick("04:00")
    assert bus.messages[0] == {"type": "night", "mode": "dim", "level": 10}, bus.messages
    assert bus.messages[1] == {"type": "control", "action": "reload"}, bus.messages
    print("SCHEDULER OK:", bus.messages)


async def check_display_state_relay() -> None:
    async with websockets.connect("ws://127.0.0.1:8080/ws/admin") as admin:
        await admin.recv()  # snapshot
        async with websockets.connect("ws://127.0.0.1:8080/ws/display") as display:
            await display.recv()  # snapshot
            await display.send(
                json.dumps(
                    {
                        "type": "display_state",
                        "state": {"module": "markets", "pinned": True, "blanked": False},
                    }
                )
            )
            while True:
                msg = json.loads(await asyncio.wait_for(admin.recv(), timeout=10))
                if msg["type"] == "display_state":
                    assert msg["state"]["module"] == "markets", msg
                    print("DISPLAY_STATE RELAY OK:", msg["state"])
                    break
    # late-joining admin gets it in the snapshot
    async with websockets.connect("ws://127.0.0.1:8080/ws/admin") as admin:
        snapshot = json.loads(await admin.recv())
        assert snapshot["display_state"]["module"] == "markets", snapshot["display_state"]
        print("DISPLAY_STATE IN SNAPSHOT OK")


async def check_astro_live() -> None:
    collector = AstroCollector({"modules": {}})
    payload = collector.shape(await collector.fetch())
    stage = payload.stage
    assert stage["moon"]["phase"], stage["moon"]
    assert 0 <= stage["moon"]["illumination"] <= 100
    assert stage["targets"], "no targets for this month"
    print(
        f"ASTRO OK: {len(stage['hours'])} night hours, avg cloud {stage['avg_cloud']}%, "
        f"moon {stage['moon']['illumination']}% {stage['moon']['phase']}, "
        f"targets {stage['targets']}"
    )
    # phase sanity at known dates: 2000-01-06 was a new moon, +14.77d ≈ full
    name_new, illum_new = moon_phase(datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc))
    name_full, illum_full = moon_phase(datetime(2000, 1, 21, 4, 40, tzinfo=timezone.utc))
    assert illum_new < 5 and name_new == "New moon", (name_new, illum_new)
    assert illum_full > 95 and name_full == "Full moon", (name_full, illum_full)
    print("MOON PHASE MATH OK")


def check_proxmox_shape() -> None:
    os.environ.update(
        {"PVE_URL": "https://pve:8006", "PVE_TOKEN_ID": "t@pam!x", "PVE_TOKEN_SECRET": "s"}
    )
    collector = ProxmoxCollector({"modules": {}})
    payload = collector.shape(
        [
            {"type": "node", "node": "pve1", "status": "online", "cpu": 0.42,
             "mem": 8 * 2**30, "maxmem": 16 * 2**30, "uptime": 200000},
            {"type": "qemu", "status": "running"},
            {"type": "lxc", "status": "stopped"},
            {"type": "storage", "storage": "local-zfs", "node": "pve1",
             "disk": 100 * 2**30, "maxdisk": 200 * 2**30},
        ]
    )
    assert payload.stage["nodes"][0]["cpu"] == 42.0
    assert payload.stage["guests"] == {"running": 1, "total": 2}
    assert payload.stage["storage"][0]["pct"] == 50.0
    assert payload.tape[0].text.startswith("PVE pve1")
    print("PROXMOX SHAPE OK:", payload.tape[0].text)


def check_adsb_shape() -> None:
    collector = AdsbCollector({"modules": {"weather": {"latitude": 27.9659, "longitude": -82.8001}}})
    payload = collector.shape(
        {
            "aircraft": [
                {"hex": "a1", "flight": "DAL123 ", "lat": 28.0, "lon": -82.8,
                 "alt_baro": 12000, "gs": 320, "track": 90},
                {"hex": "a2", "lat": 30.0, "lon": -80.0},  # far away, filtered
                {"hex": "a3"},  # no position, skipped
            ]
        }
    )
    stage = payload.stage
    assert stage["count_in_radius"] == 1 and stage["count_total"] == 3, stage
    aircraft = stage["aircraft"][0]
    assert aircraft["flight"] == "DAL123" and aircraft["distance_km"] < 10, aircraft
    print("ADSB SHAPE OK:", payload.tape[0].text)


async def main() -> None:
    await check_scheduler()
    await check_display_state_relay()
    await check_astro_live()
    check_proxmox_shape()
    check_adsb_shape()
    print("ALL UNIT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
