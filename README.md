# edge-ticker

A Glance-LED-style smart ticker for the Corsair Xeneon Edge (2560x720
touchscreen), running as a dedicated Linux kiosk appliance. Live markets,
sports, news, and weather; swipe-up Home Assistant control surface; web admin
GUI on the LAN. See [PLAN.md](PLAN.md) for the full design.

## Development (any machine)

Backend (Python ≥ 3.11):

```bash
uv sync            # installs deps only (app layout, not a package)
uv run uvicorn backend.main:app --port 8080 --reload
# without uv: python -m venv .venv, then install the [project.dependencies]
# list from pyproject.toml (deploy/install.sh shows the one-liner)
```

Frontend (hot reload, proxies /api and /ws to the backend):

```bash
cd frontend
npm install
npm run dev        # display: http://localhost:5173/display/  admin: /admin/
```

Production-style serving from the backend (no Vite dev server):

```bash
cd frontend && npm run build
# then http://localhost:8080/display and /admin
```

Secrets go in `.env` (copy from [.env.example](.env.example)). Without
`HA_URL`/`HA_TOKEN` the HA bridge stays dormant and the overlay shows
"not configured" — everything else works.

Markets note: the keyless Yahoo Finance path gets rate-limited (HTTP 429) on
some networks — Yahoo fingerprints non-browser clients. If the markets module
sits stale, grab a free key at finnhub.io and set `FINNHUB_KEY` in `.env`;
the collector switches to Finnhub REST quotes *and* streams real-time prices
over the Finnhub WebSocket between polls.

Stretch modules (Proxmox node stats, ADS-B overhead aircraft, astro
conditions) ship disabled. Enable them in the admin Modules tab, add them to
the rotation, and — for proxmox/adsb — set their env vars (see
[.env.example](.env.example)). Collectors missing required env are skipped
at startup, never errored.

Config is seeded from [config/defaults.yaml](config/defaults.yaml) into
SQLite on first boot; after that, edit via the admin page (or
`PUT /api/config`). Delete `data/ticker.db` to re-seed.

## Appliance install (Ubuntu Server 24.04)

```bash
sudo bash deploy/install.sh     # idempotent; safe to re-run
```

Installs cage + chromium, creates `ticker`/`kiosk` users, builds everything
into `/opt/edge-ticker`, enables both systemd units, and disables console
blanking. Updates later: `sudo bash deploy/update.sh`.

## Layout

- `backend/` — FastAPI app: state bus, WS channels, collectors, HA bridge
- `frontend/display/` — kiosk page: zones, rotation, tape, gestures, HA overlay
- `frontend/admin/` — LAN admin GUI
- `deploy/` — systemd units + install/update scripts
- `docs/api.md` — REST/WS contract

## Adding a module

1. Drop a `Collector` subclass in `backend/collectors/yourmodule.py`
   (see [base.py](backend/collectors/base.py) — implement `fetch()` and `shape()`).
   Backend discovery is automatic.
2. Drop a renderer in `frontend/display/modules/yourmodule.ts` that calls
   `register(...)`; import it from `main.ts` and add a `MODULE_LABELS` entry
   there (pane-header title).
3. Add the module id to `STAGE_MODULES` in
   `frontend/admin/src/tabs/modules.tsx` so the admin can add it to the
   rotation.
4. Add the module to `rotation.order` and `modules.yourmodule` in config.
