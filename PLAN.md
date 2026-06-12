# edge-ticker — Project Plan

A Glance-LED-style smart ticker for the Corsair Xeneon Edge (2560x720 touchscreen), running as a dedicated Linux kiosk appliance with live stocks, sports, news, and weather, swipe-based home automation control, and a web admin GUI accessible from any machine on the LAN.

---

## 1. Goals

- Full-screen takeover of the Xeneon Edge: no desktop, no window chrome, no OS interruptions, boots straight into the ticker.
- Rotating content modules (markets, sports, news) over a persistent status rail and an always-scrolling ticker tape.
- Touch gestures: swipe up for a Home Assistant control surface, swipe left/right to cycle modules, swipe down to dismiss/blank.
- Web admin GUI on the LAN to configure symbols, feeds, rotation, schedules, and HA entity mappings without touching the device.
- Plugin-style module system so new content types (Proxmox stats, ADS-B overhead flights, astro conditions) can be added later without touching core code.
- Appliance-grade reliability: survives reboots, network blips, and API outages; recovers without intervention.

### Non-goals (v1)

- No audio.
- No authentication beyond LAN trust for the admin GUI (add basic auth later if exposed beyond VLAN).
- No multi-display support.
- No iCUE integration (firmware updates handled by temporarily plugging into a Windows box).

---

## 2. Hardware & Host

| Item | Choice | Notes |
|---|---|---|
| Display | Corsair Xeneon Edge | 2560x720 @ 60 Hz, 14.5", ~190 PPI, capacitive touch over USB HID |
| Host | Intel N100 mini PC (16 GB / 256 GB NVMe) | ~$130–160; iGPU drives 2560x720 trivially; x86 avoids ARM packaging friction. Pi 5 is a viable fallback. |
| Connections | HDMI or USB-C DP-alt for video; USB-A/C for touch | Touch enumerates as a standard HID multitouch digitizer — no driver needed on Linux |
| OS | Ubuntu Server 24.04 LTS (minimal) | No desktop environment installed; kiosk compositor only |

Touch input note: the digitizer is a separate USB device from the video link. Both cables go to the kiosk host. Verify with `libinput list-devices` after first boot.

Brightness control: the Edge supports DDC/CI. Use `ddcutil setvcp 10 <0–100>` for scheduled dimming (night mode). Verify support with `ddcutil capabilities` early — if unsupported over the chosen video input, fall back to a software dim overlay (full-screen black div with adjustable opacity).

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  N100 mini PC (Ubuntu Server 24.04)                      │
│                                                          │
│  ┌────────────────────┐      ┌─────────────────────────┐ │
│  │ kiosk.service       │      │ ticker-backend.service  │ │
│  │ cage (Wayland)      │      │ FastAPI + uvicorn       │ │
│  │  └─ chromium --kiosk│◄────►│  ├─ REST  /api/*        │ │
│  │     http://localhost│  WS  │  ├─ WS    /ws/display   │ │
│  │     :8080/display   │      │  ├─ WS    /ws/admin     │ │
│  └────────────────────┘      │  ├─ Collectors (async)  │ │
│                               │  ├─ HA bridge (WS)      │ │
│  Admin browser (LAN) ────────►│  └─ SQLite (state/cfg)  │ │
│  http://<host>:8080/admin     └─────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
                                        │
              ┌─────────────┬───────────┼──────────────┐
              ▼             ▼           ▼              ▼
         Finnhub WS    ESPN API     RSS feeds    Home Assistant
         (stocks)      (sports)     (news)       WebSocket API
```

### Key decisions

1. **Single backend process** (FastAPI) serves the display frontend, the admin frontend, the REST API, and both WebSocket channels. One service to deploy, one port to firewall.
2. **Display is a web page** rendered in Chromium kiosk mode under `cage` (a single-app Wayland compositor). This gets GPU-composited CSS animations, trivial remote updates (refresh the page), and gesture handling via Pointer Events — no native toolkit needed.
3. **Push, don't poll, at the display layer.** Collectors poll/subscribe upstream on their own schedules; the display receives diffs over `/ws/display`. The display never talks to external APIs directly.
4. **Config lives in SQLite, edited via the admin GUI**, with a YAML seed file (`config/defaults.yaml`) for first boot and disaster recovery. Secrets (API keys, HA token) live in a `.env` file, never in the DB or repo.
5. **Module = backend collector + frontend renderer + manifest.** Both halves are discovered dynamically so adding a module never touches core files.

---

## 4. Repository layout

```
edge-ticker/
├── README.md
├── PLAN.md                      # this file
├── .env.example                 # FINNHUB_KEY=, HA_URL=, HA_TOKEN=, ...
├── pyproject.toml               # uv-managed; fastapi, uvicorn, httpx, apscheduler, websockets, pyyaml, aiosqlite
├── config/
│   └── defaults.yaml            # seed config: modules, symbols, feeds, rotation, schedules
├── backend/
│   ├── main.py                  # app factory, lifespan, static mounts
│   ├── state.py                 # central state store + diff broadcaster
│   ├── ws.py                    # /ws/display and /ws/admin handlers
│   ├── api/
│   │   ├── config.py            # GET/PUT config endpoints
│   │   ├── control.py           # POST /api/control (next, prev, pin, blank, reload)
│   │   └── ha.py                # HA entity list proxy, action dispatch
│   ├── ha_bridge.py             # persistent HA WebSocket client w/ reconnect
│   ├── collectors/
│   │   ├── base.py              # Collector ABC: name, interval, fetch(), shape()
│   │   ├── stocks.py            # Finnhub WS primary, yfinance polling fallback
│   │   ├── sports.py            # ESPN scoreboard endpoints (per-league)
│   │   ├── news.py              # RSS via feedparser (Reuters, AP, HN, user-defined)
│   │   └── weather.py           # Open-Meteo (no key) for Clearwater, FL
│   └── db.py                    # aiosqlite: config, module state cache
├── frontend/
│   ├── package.json             # Vite + TypeScript, no framework (display) — keep it lean
│   ├── vite.config.ts           # two entries: display, admin
│   ├── display/
│   │   ├── index.html
│   │   ├── main.ts              # WS client, zone manager, rotation engine
│   │   ├── gestures.ts          # pointer-event swipe/tap/long-press recognizer
│   │   ├── tape.ts              # marquee renderer (transform-based, GPU path)
│   │   ├── overlay-ha.ts        # home automation surface
│   │   ├── modules/
│   │   │   ├── registry.ts      # module renderer registration
│   │   │   ├── markets.ts
│   │   │   ├── sports.ts
│   │   │   └── news.ts
│   │   └── styles.css
│   └── admin/
│       ├── index.html           # Preact + signals (worth a small framework here)
│       └── src/...              # tabs: Modules, Symbols, Feeds, HA mapping, Schedule, System
├── deploy/
│   ├── ticker-backend.service   # systemd unit (backend)
│   ├── kiosk.service            # systemd unit (cage + chromium)
│   ├── install.sh               # idempotent setup script for fresh host
│   └── chromium-flags.txt
└── docs/
    └── api.md                   # REST/WS contract reference
```

---

## 5. Screen layout (2560x720 native)

Three persistent zones plus one overlay:

| Zone | Geometry | Content |
|---|---|---|
| Status rail | left, 440 x 600 | Clock (~96 px type), date, weather. Never rotates. |
| Module stage | 2120 x 600 | One module page at a time; auto-rotate every 20–30 s; page dots top-right. |
| Ticker tape | full width x 120, bottom | Continuous marquee mixing condensed items from all enabled modules. Never stops. |
| HA overlay | replaces stage on swipe-up | Scenes column / lights grid / climate+media column. Tape stays visible. Auto-dismiss after 30 s idle. |

Mounted-above-eye-line adjustments:
- Bias content toward the bottom half of the stage.
- Minimum glanceable type: 48 px values, 28 px labels, 96 px clock.
- Touch targets ≥ 100 px (1 cm ≈ 75 px at 190 PPI); HA tiles ≈ 480x250.
- Clock position jitter ±4 px every few minutes; scheduled dim via DDC/CI at night.

### Gesture map

| Gesture | Context | Action |
|---|---|---|
| Swipe left / right | Ticker | Cycle stage module |
| Swipe up | Ticker | Open HA overlay |
| Swipe down | Overlay open | Dismiss overlay |
| Swipe down | Ticker | Blank screen (night mode); any tap wakes |
| Tap card | Stage | Expanded detail ~20 s, then auto-return |
| Long-press | Stage | Pin/unpin current module (pauses rotation) |

Recognizer: pointer-down starts tracking; displacement > 60 px within 300 ms → swipe (dominant axis wins); otherwise tap. Long-press = 500 ms hold under 10 px movement. Once swipe threshold trips, suppress tap/click synthesis.

---

## 6. Module system

### Backend contract (`collectors/base.py`)

```python
class Collector(ABC):
    name: str                # "markets"
    interval: float          # seconds between fetches (ignored if push-based)

    async def start(self, bus): ...   # default loop: fetch -> shape -> bus.publish
    async def fetch(self) -> Any: ... # raw upstream call
    def shape(self, raw) -> ModulePayload: ...
```

`ModulePayload` (pydantic): `module`, `updated_at`, `stage` (full-page data), `tape` (list of condensed tape items: `text`, `accent`, `priority`). The state store keeps the latest payload per module and broadcasts JSON diffs to `/ws/display` subscribers.

Failure policy: collectors never crash the app. On upstream failure, keep last-good payload, mark it `stale: true` (display renders a subtle staleness dot), retry with exponential backoff capped at 5 min.

### Frontend contract (`display/modules/registry.ts`)

```ts
interface ModuleRenderer {
  id: string;
  renderStage(el: HTMLElement, data: StagePayload): void;
  renderDetail?(el: HTMLElement, item: unknown): void;  // tap-to-expand
}
```

Rotation engine reads the enabled-module order from config, swaps stage content with a 300 ms crossfade, updates page dots.

### v1 data sources

| Module | Primary | Fallback / notes |
|---|---|---|
| Markets | Finnhub WebSocket (free tier, real-time US equities + crypto) | yfinance polling every 60 s. Show market open/closed state; freeze sparkline after close. |
| Sports | ESPN public scoreboard JSON (`site.api.espn.com/apis/site/v2/sports/...`) | Per-league polling: 30 s during live games, 10 min otherwise. Config: followed teams (Rays, Bucs, Lightning) pinned first. |
| News | RSS via feedparser — Reuters, AP, Ars Technica, user-defined feeds | Poll 5 min, dedupe by GUID, keep newest 30. Stage shows headline + source + age; tape shows headline only. |
| Weather | Open-Meteo (keyless) for Clearwater, FL | Poll 15 min. Feeds the status rail, not a stage page. |

Stretch modules (post-v1): Proxmox node stats (existing PVE API), ADS-B overhead aircraft (existing dump1090 JSON at `/data/aircraft.json`), astro conditions (cloud cover + moon phase + tonight's imaging targets — Open-Meteo cloud layers).

---

## 7. Home Assistant integration

- `ha_bridge.py` holds a persistent connection to `HA_URL/api/websocket`, authenticating with a long-lived access token (`HA_TOKEN` in `.env`).
- Subscribes to `state_changed` for mapped entities only; pushes entity state diffs to the display so tiles update live (someone flips a light at the wall → tile updates).
- Display tile taps POST to `/api/ha/action` → bridge issues `call_service` (e.g. `light.toggle`, `scene.turn_on`, `climate.set_temperature`).
- Admin GUI "HA mapping" tab lists all entities fetched from HA, lets you assign: scenes column (max 4), lights grid (max 8), climate entity, media entity.
- Reconnect with backoff; if HA is down, overlay tiles render disabled with a "HA unreachable" banner.

---

## 8. API surface (summary)

REST (`/api`):
- `GET /config`, `PUT /config` — full config document (admin GUI)
- `POST /control` — `{action: "next"|"prev"|"pin"|"blank"|"wake"|"reload"}` (also drives the display remotely from admin)
- `GET /ha/entities`, `POST /ha/action`
- `GET /health` — collector status, last-update ages, HA connection state

WebSockets:
- `/ws/display` — server→client: module payload diffs, config changes, control commands, HA entity states. Client→server: gesture-originated actions (module change, HA action), heartbeat.
- `/ws/admin` — live preview of what the display is showing + health stream.

Full schemas to be documented in `docs/api.md` as they stabilize.

---

## 9. Kiosk & deployment

### `deploy/kiosk.service` (sketch)

```ini
[Unit]
Description=Edge ticker kiosk
After=ticker-backend.service
Wants=ticker-backend.service

[Service]
User=kiosk
ExecStart=/usr/bin/cage -- /usr/bin/chromium \
  --kiosk --noerrdialogs --disable-infobars \
  --disable-session-crashed-bubble --autoplay-policy=no-user-gesture-required \
  --ozone-platform=wayland \
  http://127.0.0.1:8080/display
Restart=always
RestartSec=3

[Install]
WantedBy=graphical.target
```

### `deploy/ticker-backend.service` (sketch)

```ini
[Service]
User=ticker
WorkingDirectory=/opt/edge-ticker
EnvironmentFile=/opt/edge-ticker/.env
ExecStart=/opt/edge-ticker/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3
```

### `install.sh` responsibilities (idempotent)

1. apt: `cage chromium ddcutil python3-venv git` (+ `libinput-tools` for touch debugging)
2. Create `kiosk` and `ticker` users; add `kiosk` to `video`/`input` groups
3. Clone/pull repo to `/opt/edge-ticker`, `uv sync` (or venv + pip), `npm ci && npm run build` in `frontend/`
4. Copy systemd units, `daemon-reload`, enable both
5. Disable console blanking and unwanted suspend targets

### Operational details

- Display refresh after deploy: backend bumps a `version` field over `/ws/display`; the page hard-reloads itself. No SSH-to-restart-browser needed.
- Watchdog: display sends WS heartbeat every 10 s; `kiosk.service` `Restart=always` covers Chromium crashes; a small systemd timer curls `/health` and restarts the backend on repeated failure.
- Night schedule (admin-configurable): DDC/CI dim at 23:00 → 10 %, restore 07:00; optional full blank with tap-to-wake.
- Updates: `git pull && npm run build && systemctl restart ticker-backend` wrapped in `deploy/update.sh`. (CI/CD via GitHub Actions self-hosted runner is a stretch goal.)

---

## 10. Build phases

### Phase 0 — Hardware & OS (half a day)
Provision N100, Ubuntu Server 24.04 minimal, static IP/DHCP reservation, SSH keys. Connect Edge (video + USB touch). Verify: `libinput list-devices` shows the digitizer; `ddcutil detect` sees the panel; a test page renders at 2560x720.
**Done when:** `cage -- chromium --kiosk https://example.com` fills the panel and touch moves the cursor.

### Phase 1 — Kiosk shell + static layout (1–2 evenings)
Repo scaffold, Vite frontend with the three-zone layout and hardcoded sample data, both systemd units, install.sh. Marquee tape running via CSS transform animation.
**Done when:** host boots unattended into the static ticker; survives power pull.

### Phase 2 — Backend core + first module (2–3 evenings)
FastAPI app, state store, `/ws/display`, collector base, markets collector (yfinance polling first — simplest), display consumes live data. Health endpoint.
**Done when:** real quotes update on the panel without page refresh; killing the network shows stale indicators and recovers.

### Phase 3 — Full rotation (2 evenings)
Sports + news + weather collectors, rotation engine, page dots, tape aggregation across modules, tap-to-expand detail views.
**Done when:** all three stage modules rotate with live data and the tape mixes all sources.

### Phase 4 — Touch gestures + HA (2–3 evenings)
Gesture recognizer, HA bridge, overlay surface, entity state live-sync, swipe-down blank/wake.
**Done when:** swipe up → toggle a real light → tile reflects state; wall-switch changes appear on the panel within ~1 s.

### Phase 5 — Admin GUI (2–3 evenings)
Preact admin app: module order/enable, symbol & team & feed lists, rotation timing, night schedule, HA mapping, live preview, remote control buttons, health dashboard.
**Done when:** every config change applies to the display within seconds, no SSH involved.

### Phase 6 — Hardening & polish (ongoing)
Upgrade markets to Finnhub WS, burn-in jitter, DDC/CI scheduled dimming, watchdog timer, staleness UX, update.sh, README with photos.
**Done when:** it runs for two weeks untouched.

### Phase 7 — Stretch modules
Proxmox stats, ADS-B "overhead now" cards, astro conditions / tonight's targets, Rewst/ConnectWise alert hooks via webhook-ingest module.

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Touch panel quirks under Wayland (offset/rotation mapping) | `cage` maps single-output touch automatically; if offsets appear, fix with a libinput calibration matrix udev rule. Test in Phase 0. |
| Finnhub free-tier limits or WS drops | yfinance polling fallback baked into the collector from day one; tape tolerates 60 s granularity. |
| ESPN endpoints are unofficial and can change | Isolate all parsing in `sports.py::shape()`; failures degrade to hiding the module, never crash. |
| DDC/CI unsupported over USB-C DP-alt input | Software dim overlay fallback; test both inputs in Phase 0. |
| Chromium memory creep over weeks | Nightly scheduled page reload at 04:00 (config flag) + `Restart=always`. |
| HA token leakage | `.env` is root-readable only, gitignored; admin GUI never displays the token. |

---

## 12. First commands

```bash
mkdir edge-ticker && cd edge-ticker && git init
# drop this file in as PLAN.md
gh repo create edge-ticker --private --source=. --push
```

Then work the phases top to bottom — each one ends in something that runs on the panel.
