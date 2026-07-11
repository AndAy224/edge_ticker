# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A smart-ticker kiosk appliance for a Corsair Xeneon Edge (2560×720 touchscreen): FastAPI backend pushing live data (markets, sports/fantasy, news, a weather suite incl. animated radar and NHC hurricane tracking, rocket launches, astro, Proxmox, ADS-B, Home Assistant) over WebSockets to a Chromium-in-cage display page, plus a LAN admin GUI. Production runs from `/opt/edge-ticker` as `ticker-backend.service` on `:8080`; this checkout is the dev copy.

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

**Data pipeline:** each module is a backend `Collector` subclass (`backend/collectors/`, contract in `base.py`: `fetch()` upstream → `shape()` into a `ModulePayload` with `stage` dict + `tape` items) publishing to the in-memory `Bus` (`backend/state.py`), which fans out to all WebSocket clients (`/ws/display`, `/ws/admin` — same stream). Collectors never crash the app: failures re-publish the last payload with `stale=True` and back off. Backend discovery is automatic (drop a file in `collectors/`); the frontend needs three touchpoints per new module: import in `display/main.ts`, a `MODULE_LABELS` entry there (pane-header title), and the `STAGE_MODULES` list in `admin/src/tabs/modules.tsx`. Collectors with dynamic cadence mutate `self.interval` inside `shape()` (sports/fantasy live-vs-idle, launches near T-0). Existing config DBs never gain new default keys, so both collectors and admin patches must default gracefully — admin uses the spread-patch idiom (`c.modules.x = { ...(c.modules.x ?? {}), key: value }`, see `sources.tsx`).

**Frontend display** (`frontend/display/`, Preact-free vanilla TS): `main.ts` owns the WS client, rotation engine, and a **multi-pane stage** — layouts can show 1–3 consecutive rotation modules side by side (`panes` field in `LAYOUTS`). Each pane holds crossfading `.stage-layer`s plus a persistent `.pane-header`; `crossfade()` must only remove `.stage-layer` children. Module renderers register in `modules/registry.ts` (`renderStage`, optional `renderDetail`/`getDetailItem` for tap-to-expand). Open details freeze (panes skip re-render) — anything needing live refresh must render in `renderStage` (that's why live game mode lives inside the sports renderer, and the launches T-0 board inside that renderer). **Renderers have no teardown hook**: animation must be CSS (radar frame loop, ADS-B sweep) or a JS interval that clears itself when `!el.isConnected` (launches countdown). Tape items are static strings until the collector republishes — no client-side relative time. Cross-module data reaches a renderer via an exported setter fed by `main.ts` (`setWeatherAlerts`, `setSportsLiveMode`, `setLaunchSun`). Live events auto-pin via the edge-triggered `applyAutoFeature`/`clearAutoFeature` pair in `main.ts` (sports/fantasy/launches; opt-in per module via `modules.<id>.auto_feature`).

**Map modules** (weather_radar, hurricanes) share `frontend/display/slippymap.ts` — Web-Mercator math, tile mosaics, `fitView`, and fractional zoom (tiles at the nearest integer level, CSS `scale()` about the view center for the remainder; overlays inside the scaled container need `vector-effect: non-scaling-stroke`, crisp text chips go outside it at post-scale coordinates). They also share the `.radar-viewport`/`.radar-map`/`.radar-basemap`/`.radar-home` CSS plumbing.

**Appearance:** themes are CSS-variable bundles + a `data-theme` attribute; layouts are `data-layout` grid variants — both defined once in `frontend/shared/themes.ts` (shared by display and admin selectors) and applied by `applyAppearance()`. Structural elements used by only some themes (pane headers, weather location, HA overlay titles) exist in every DOM but are `display:none` until a theme reveals them — the existing themes must stay pixel-identical when adding such elements. `data-panes` on `<html>` lets module CSS adapt to narrow panes (single-column lists, row caps — stage layers are bottom-aligned, so oversized content overflows out the top).

**On-demand detail proxies:** `backend/api/sports.py` and `backend/api/markets.py` curate ESPN/Finnhub per-item detail (win probability, standings, headlines, analyst ratings…) with small TTL caches; the display fetches them only on tap (`enrichDetail` pattern: render base synchronously, fetch, patch if `el.isConnected`). Never poll these.

**Events beyond module payloads:** the sports collector diffs followed-game scores between polls and broadcasts `sport_event` → full-screen celebration overlay (`celebrate.ts`: field scene → fireworks → scorer card; queued, tap-dismiss). HA alert entities ride the bridge's mapped-state broadcasts → tape items while matched + transition toasts. Score celebrations are throttled by a **sport-aware per-team cooldown** (`SPORT_COOLDOWN_SECONDS` in `sports.py`): short for discrete scoring (baseball/hockey, so every run/goal fires), long for basketball — the score-diff already fires only on an increase, so a blanket long cooldown silently drops legitimate runs. `POST /api/control {"action":"celebrate_test"}` replays a real Packers touchdown — note it's **football-only**, so it proves the overlay renders but exercises neither per-sport rendering nor the live score-diff path (a passing "test works" does not mean baseball works).

**External-API constraints (hard-won):** Finnhub free tier has no candles (sparklines are self-built from accumulated quote history, persisted to `data/markets-spark.json`) and no crypto REST (`-USD` symbols poll Coinbase's keyless API; Binance is US-geo-blocked); one Finnhub WebSocket per key (dev and prod fight over it — prod wins). Yahoo 429s this network. ESPN scoring plays: `scoringPlays[]` for football/hockey, `plays[].scoringPlay` for MLB; NFL plays carry no participant ids — scorer is resolved by earliest boxscore-name match in the play text. **MLB scoring plays carry `team:{id}` with no abbreviation — match the followed team's play by team *id*, not abbreviation (`latest_scoring_play(..., team_id=)`), or the celebration shows the opponent's scorer/play.** IPO-day quotes have no previous close — measure against the open. RainViewer radar tiles cap at **z7** (higher zooms return "Zoom Level Not Supported" placeholder PNGs that still 200 — check for the 4-bit-colormap tile, not the status); the display upscales radar over a sharper Carto basemap via `frontend/display/slippymap.ts` fractional zoom. Launch Library 2 free tier is ~15 req/hr **per IP — dev and prod share it**, so don't leave a dev backend polling launches, and a 429 must be retried slowly (the launches collector overrides `backoff_start`/`backoff_max` to 15–60 min; the default 5s..300s retry loop alone would exceed the budget and never recover). NHC storm geometry comes from per-storm KMZ products (zipped KML, stdlib-parsed in `collectors/hurricanes.py`), re-fetched only on advisory-number change.

## Appliance quirks (matter for any display work)

- **No emoji fonts** on the device — all icons must be inline SVG (`frontend/display/icons.ts`, `stroke="currentColor"`).
- Chromium is the **snap** (`/snap/bin/chromium`); there is no `/usr/bin/chromium`. Snap confinement redirects `/tmp` writes — headless screenshots must target `$HOME`.
- `kiosk.service` needs the PAM/logind/seat0 setup and the udev rule in `deploy/` (the touch controller exposes a mouse node that otherwise draws a cursor). `watchdog.sh` restarts the backend on failed health checks and the kiosk when `display_clients` stays 0.
- Config lives in SQLite (`data/ticker.db`), seeded once from `config/defaults.yaml`; live edits go through `PUT /api/config`, which restarts collectors and broadcasts to all clients. Existing DBs don't get new default keys — code must default gracefully.
- Secrets in `.env` (prod: `/opt/edge-ticker/.env`, root-owned — editing the dev checkout's `.env` does not affect prod).

## Verification workflow (established pattern)

1. Run the dev backend on `:8081` and probe with short Python WS/HTTP scripts (`websockets`/`httpx` are in the venv).
2. Visual checks: `/snap/bin/chromium --headless=new --window-size=2560,720 --virtual-time-budget=9000 --screenshot=$HOME/... http://127.0.0.1:8081/display`; for interactions, attach CDP (`--remote-debugging-port`) and dispatch **pointer** events (`gestures.ts` ignores synthetic `.click()`).
3. Debug hooks on the display window for injecting state: `__rotshow(id)` (jump+pin a module into pane 0), `__modfake(id, stage)` (replace any payload), plus side-effect-aware fakes `__sportsfake(games)`, `__fantasyfake(stage)`, `__launchfake(stage)`, `__adsbfake(stage)`, and event triggers `__celebrate(event)`, `__fantasyevent(event)`, `__scorechip(game)`, `__weatheralert(alert)`, `__hatest(entity, state, name)`, `__starshiptest()`. Canned display tests also exist as control actions (`celebrate_test`, `weather_alert_test`, `starship_test`) with buttons in the admin System tab.
4. Mutate the dev config via `PUT /api/config` for the scenario (rotation order, theme/layout, followed teams) and restore it afterward.
5. To inspect the **live kiosk** (not dev `:8081`) it has no DevTools port by default — add one with a `/etc/systemd/system/kiosk.service.d/*.conf` drop-in that re-declares `ExecStart` with `--remote-debugging-port=9222` (binds localhost only), `daemon-reload` + `restart kiosk`, then attach CDP to `:9222`: the page exposes the same debug hooks plus `Page.captureScreenshot` of the *real panel*. Delete the drop-in to revert. Confirmed this way: a live `__celebrate(baseballEvent)` renders fine on the kiosk and fires **zero** stray `pointerdown`s (the udev mouse-ignore rule holds) — so "no celebration" was the cooldown + wrong-scorer, not the display.
6. Scripting the snap chromium from a shell: `export XDG_RUNTIME_DIR=/run/user/$(id -u)` (else it dies silently, exit 144), put `--user-data-dir` under `~/snap/chromium/common/` (AppArmor denies arbitrary paths), and spawn it as a **foreground child** — a background job gets killed. `sudo` has no TTY in the non-interactive tool shell, so run privileged steps (deploy, kiosk restart) yourself via the `!` prompt prefix.
