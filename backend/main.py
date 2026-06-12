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
log = logging.getLogger("ticker")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import db  # noqa: E402
from .api.config import router as config_router  # noqa: E402
from .api.control import router as control_router  # noqa: E402
from .api.ha import router as ha_router  # noqa: E402
from .api.markets import router as markets_router  # noqa: E402
from .api.sports import router as sports_router  # noqa: E402
from .collectors import discover_collectors  # noqa: E402
from .ha_bridge import HABridge  # noqa: E402
from .scheduler import NightScheduler  # noqa: E402
from .state import Bus  # noqa: E402
from . import ws as ws_channels  # noqa: E402
from .ws import router as ws_router  # noqa: E402

DIST = ROOT / "frontend" / "dist"


class CollectorManager:
    """Owns collector tasks so a config change can restart them cleanly."""

    def __init__(self) -> None:
        self.collectors: list = []
        self._tasks: list[asyncio.Task] = []

    async def start(self, bus: Bus, config: dict) -> None:
        self.collectors = discover_collectors(config)
        self._tasks = [
            asyncio.create_task(c.start(bus), name=f"collector:{c.name}")
            for c in self.collectors
        ]
        log.info("collectors running: %s", [c.name for c in self.collectors])

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def restart(self, bus: Bus, config: dict) -> None:
        await self.stop()
        await self.start(bus, config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    config = await db.get_config()
    bus = Bus()
    manager = CollectorManager()
    bridge = HABridge(bus, lambda: app.state.config)

    app.state.bus = bus
    app.state.config = config
    app.state.manager = manager
    app.state.ha = bridge

    await manager.start(bus, config)
    scheduler = NightScheduler(bus, lambda: app.state.config)
    background = [
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
app.include_router(config_router, prefix="/api")
app.include_router(control_router, prefix="/api")
app.include_router(ha_router, prefix="/api")
app.include_router(markets_router, prefix="/api")
app.include_router(sports_router, prefix="/api")
app.include_router(ws_router)


@app.get("/health")
@app.get("/api/health")
def health(request: Request) -> dict:
    bus: Bus = request.app.state.bus
    bridge: HABridge = request.app.state.ha
    return {
        "ok": True,
        "collectors": [c.status() for c in request.app.state.manager.collectors],
        "ha": bridge.status,
        "ws_clients": bus.subscriber_count,
        "display_clients": ws_channels.display_clients,
    }


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/display")


def _page(name: str):
    index = DIST / name / "index.html"
    if index.exists():
        return FileResponse(index)
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
