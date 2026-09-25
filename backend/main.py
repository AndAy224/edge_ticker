"""App factory: lifespan wiring, REST routers, WebSockets, static frontend mounts."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader for dev runs; systemd's EnvironmentFile covers prod."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
# httpx logs every request URL at INFO: ~1k journal lines an hour that bury the
# real warnings, and any credential carried in a query string along with them.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ticker")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import db  # noqa: E402
from .api.adsb import router as adsb_router  # noqa: E402
from .build import builds, git_revision  # noqa: E402
from .api.cameras import router as cameras_router  # noqa: E402
from .api.config import router as config_router  # noqa: E402
from .api.control import router as control_router  # noqa: E402
from .api.fantasy import router as fantasy_router  # noqa: E402
from .api.ha import router as ha_router  # noqa: E402
from .api.markets import router as markets_router  # noqa: E402
from .api.sports import router as sports_router  # noqa: E402
from .camera_alert import CameraAlertHub  # noqa: E402
from .ha_bridge import HABridge  # noqa: E402
from .manager import CollectorManager  # noqa: E402
from .origin import UNSAFE_METHODS, cross_site  # noqa: E402
from .scheduler import NightScheduler  # noqa: E402
from .state import Bus  # noqa: E402
from .webrtc import WebRTCRelay  # noqa: E402
from . import ws as ws_channels  # noqa: E402
from .ws import router as ws_router  # noqa: E402

DIST = ROOT / "frontend" / "dist"
REVISION = git_revision()  # read once: the code on disk may move under a running process

# Fixture mode, for display work and layout audits: serve module payloads
# recorded in a snapshot file (the `modules` map of a WS snapshot) instead of
# polling upstream. Runs no collectors — dev and prod share Launch Library's
# per-IP budget and Finnhub's one-socket-per-key — no night scheduler (it drives
# the real panel's brightness over DDC) and no HA bridge.
FIXTURE = os.environ.get("TICKER_FIXTURE", "").strip()


def _load_fixture(bus: Bus, path: str) -> None:
    import json

    from .state import ModulePayload

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for name, payload in (data.get("modules") or data).items():
        bus.payloads[name] = ModulePayload.model_validate(payload)
    log.info("fixture mode: %d module payloads from %s", len(bus.payloads), path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    config = db.with_defaults(await db.get_config())
    bus = Bus()
    manager = CollectorManager(enabled=not FIXTURE)
    if FIXTURE:
        _load_fixture(bus, FIXTURE)
    bridge = HABridge(bus, lambda: app.state.config)
    scheduler = NightScheduler(bus, lambda: app.state.config)
    camera_alerts = CameraAlertHub(
        bus,
        lambda: app.state.config,
        friendly_name=lambda eid: (
            (bridge.all_states.get(eid) or {}).get("attributes", {}).get("friendly_name")
            or eid
        ),
        scheduler=scheduler,
    )
    bridge.camera_alerts = camera_alerts
    webrtc = WebRTCRelay(bridge, lambda: app.state.config)

    app.state.bus = bus
    app.state.config = config
    app.state.manager = manager
    app.state.ha = bridge
    app.state.scheduler = scheduler
    app.state.camera_alerts = camera_alerts
    app.state.webrtc = webrtc

    await manager.apply(bus, config)
    background = [] if FIXTURE else [
        asyncio.create_task(bridge.run(), name="ha-bridge"),
        asyncio.create_task(scheduler.run(), name="night-scheduler"),
    ]
    yield
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await manager.stop()
    await db.close()


app = FastAPI(title="edge-ticker", lifespan=lifespan)


@app.middleware("http")
async def refuse_cross_site_writes(request: Request, call_next):
    if request.method in UNSAFE_METHODS and cross_site(
        request.headers.get("origin"), request.headers.get("host")
    ):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    return await call_next(request)

app.include_router(adsb_router, prefix="/api")
app.include_router(cameras_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(control_router, prefix="/api")
app.include_router(fantasy_router, prefix="/api")
app.include_router(ha_router, prefix="/api")
app.include_router(markets_router, prefix="/api")
app.include_router(sports_router, prefix="/api")
app.include_router(ws_router)


@app.get("/health")
@app.get("/api/health")
def health(request: Request) -> dict:
    bus: Bus = request.app.state.bus
    bridge: HABridge = request.app.state.ha
    collectors = request.app.state.manager.status()
    scheduler: NightScheduler = request.app.state.scheduler
    return {
        # `ok` means the backend itself is alive — an upstream outage is not a
        # reason for the watchdog to restart it. What a restart *does* fix is
        # listed in `stuck`; everything else worth a look is in `problems`.
        "ok": True,
        "stuck": [c["name"] for c in collectors if c.get("stuck")],
        "problems": _problems(collectors),
        "collectors": collectors,
        "ha": bridge.status,
        "night": scheduler.state() | {"method_used": scheduler.method_used},
        "ws_clients": bus.subscriber_count,
        "display_clients": ws_channels.display_clients,
        "dropped_messages": bus.dropped,
        "fixture": bool(FIXTURE),
        "revision": REVISION,
        "build": builds(),
    }


def _problems(collectors: list[dict]) -> list[str]:
    out = []
    for c in collectors:
        name = c["name"]
        if c.get("state") == "error":
            out.append(f"{name}: config rejected — {c.get('detail')}")
        elif c.get("state") == "dead":
            out.append(f"{name}: collector loop has exited")
        elif c.get("stuck"):
            out.append(f"{name}: no poll attempt completing")
        elif c.get("overdue"):
            out.append(f"{name}: no fresh data since {c.get('last_success') or 'startup'}")
        elif c.get("degraded"):
            out.append(f"{name}: {c['degraded']}")
    return out


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/display")


def _page(name: str):
    index = DIST / name / "index.html"
    if index.exists():
        # Always revalidated: the hashed assets it links are what a deploy
        # changes, and a heuristically cached copy would reload into the old build.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse(
        {
            "error": f"frontend not built — run `npm run build` in frontend/, "
            f"or use the Vite dev server (npm run dev) for /{name} during development"
        },
        status_code=503,
    )


@app.get("/display", include_in_schema=False)
def display_page():
    return _page("display")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return _page("admin")


if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")
