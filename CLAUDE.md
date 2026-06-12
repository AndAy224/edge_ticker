# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A smart-ticker kiosk appliance for a Corsair Xeneon Edge (2560×720 touchscreen): FastAPI backend pushing live data (markets, sports, news, weather, astro, Proxmox, ADS-B) over WebSockets to a Chromium-in-cage display page, plus a LAN admin GUI. Production runs from `/opt/edge-ticker` as `ticker-backend.service` on `:8080`; this checkout is the dev copy.

## Commands

```bash
# Backend (Python ≥3.11; this box has no uv — use the venv)
.venv/bin/uvicorn backend.main:app --port 8081      # dev backend (8080 is prod)
.venv/bin/python -c "import backend.main"            # fast import check

# Frontend (from frontend/)
npx tsc --noEmit        # typecheck (strict; runs over display/, admin/, shared/)
npm run build           # vite build → frontend/dist (served by the backend)
npm run dev             # hot-reload dev server, proxies /api + /ws to :8080

# Deploy (on the appliance; pulls main ff-only — push first)
sudo bash deploy/update.sh
curl -X POST localhost:8080/api/control -H 'Content-Type: application/json' -d '{"action":"reload"}'
```

There is no test suite; verification is probe-and-screenshot driven (see below).

## Architecture

**Data pipeline:** each module is a backend `Collector` subclass (`backend/collectors/`, contract in `base.py`: `fetch()` upstream → `shape()` into a `ModulePayload` with `stage` dict + `tape` items) publishing to the in-memory `Bus` (`backend/state.py`), which fans out to all WebSocket clients (`/ws/display`, `/ws/admin` — same stream). Collectors never crash the app: failures re-publish the last payload with `stale=True` and back off. Discovery is automatic; adding a module touches no core files (see README).

**Frontend display** (`frontend/display/`, Preact-free vanilla TS): `main.ts` owns the WS client, rotation engine, and a **multi-pane stage** — layouts can show 1–3 consecutive rotation modules side by side (`panes` field in `LAYOUTS`). Each pane holds crossfading `.stage-layer`s plus a persistent `.pane-header`; `crossfade()` must only remove `.stage-layer` children. Module renderers register in `modules/registry.ts` (`renderStage`, optional `renderDetail`/`getDetailItem` for tap-to-expand). Open details freeze (panes skip re-render) — anything needing live refresh must render in `renderStage` (that's why live game mode lives inside the sports renderer).

**Appearance:** themes are CSS-variable bundles + a `data-theme` attribute; layouts are `data-layout` grid variants — both defined once in `frontend/shared/themes.ts` (shared by display and admin selectors) and applied by `applyAppearance()`. Structural elements used by only some themes (pane headers, weather location, HA overlay titles) exist in every DOM but are `display:none` until a theme reveals them — the existing themes must stay pixel-identical when adding such elements. `data-panes` on `<html>` lets module CSS adapt to narrow panes (single-column lists, row caps — stage layers are bottom-aligned, so oversized content overflows out the top).

**On-demand detail proxies:** `backend/api/sports.py` and `backend/api/markets.py` curate ESPN/Finnhub per-item detail (win probability, standings, headlines, analyst ratings…) with small TTL caches; the display fetches them only on tap (`enrichDetail` pattern: render base synchronously, fetch, patch if `el.isConnected`). Never poll these.

**Events beyond module payloads:** the sports collector diffs followed-game scores between polls and broadcasts `sport_event` → full-screen celebration overlay (`celebrate.ts`: field scene → fireworks → scorer card; queued, tap-dismiss). HA alert entities ride the bridge's mapped-state broadcasts → tape items while matched + transition toasts. `POST /api/control {"action":"celebrate_test"}` replays a real Packers touchdown.

**External-API constraints (hard-won):** Finnhub free tier has no candles (sparklines are self-built from accumulated quote history, persisted to `data/markets-spark.json`) and no crypto REST (`-USD` symbols poll Coinbase's keyless API; Binance is US-geo-blocked); one Finnhub WebSocket per key (dev and prod fight over it — prod wins). Yahoo 429s this network. ESPN scoring plays: `scoringPlays[]` for football/hockey, `plays[].scoringPlay` for MLB; NFL plays carry no participant ids — scorer is resolved by earliest boxscore-name match in the play text. IPO-day quotes have no previous close — measure against the open.

## Appliance quirks (matter for any display work)

- **No emoji fonts** on the device — all icons must be inline SVG (`frontend/display/icons.ts`, `stroke="currentColor"`).
- Chromium is the **snap** (`/snap/bin/chromium`); there is no `/usr/bin/chromium`. Snap confinement redirects `/tmp` writes — headless screenshots must target `$HOME`.
- `kiosk.service` needs the PAM/logind/seat0 setup and the udev rule in `deploy/` (the touch controller exposes a mouse node that otherwise draws a cursor). `watchdog.sh` restarts the backend on failed health checks and the kiosk when `display_clients` stays 0.
- Config lives in SQLite (`data/ticker.db`), seeded once from `config/defaults.yaml`; live edits go through `PUT /api/config`, which restarts collectors and broadcasts to all clients. Existing DBs don't get new default keys — code must default gracefully.
- Secrets in `.env` (prod: `/opt/edge-ticker/.env`, root-owned — editing the dev checkout's `.env` does not affect prod).

## Verification workflow (established pattern)

1. Run the dev backend on `:8081` and probe with short Python WS/HTTP scripts (`websockets`/`httpx` are in the venv).
2. Visual checks: `/snap/bin/chromium --headless=new --window-size=2560,720 --virtual-time-budget=9000 --screenshot=$HOME/... http://127.0.0.1:8081/display`; for interactions, attach CDP (`--remote-debugging-port`) and dispatch **pointer** events (`gestures.ts` ignores synthetic `.click()`).
3. Debug hooks on the display window for injecting state: `__celebrate(event)`, `__scorechip(game)`, `__sportsfake(games)`, `__hatest(entity, state, name)`.
4. Mutate the dev config via `PUT /api/config` for the scenario (rotation order, theme/layout, followed teams) and restore it afterward.
