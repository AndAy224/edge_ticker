# edge-ticker — API reference

Single backend on port 8080 serves both frontends, the REST API, and both
WebSocket channels.

Access model: LAN trust, no login. Two guards keep that from stretching to
"any web page a LAN user opens": state-changing requests (`POST`/`PUT`/
`PATCH`/`DELETE`) and WebSocket handshakes whose `Origin` names a different
host than they were sent to are refused (`403` / close `1008`) — requests with
no `Origin` (curl, scripts) pass; and Home Assistant service calls are limited
to the mapped controls (see `POST /api/ha/action`).

## Pages

| Path | What |
|---|---|
| `/display` | Kiosk display page (Chromium on the panel) |
| `/admin` | Admin GUI (any LAN browser) |

## REST

### `GET /api/health` (alias `/health`)

```json
{
  "ok": true,
  "stuck": [],
  "problems": ["sports: schedule window: 3/12 days failing"],
  "collectors": [
    {
      "name": "markets",
      "state": "running",
      "interval": 60,
      "stale": false,
      "overdue": false,
      "stuck": false,
      "degraded": null,
      "last_success": "2026-06-11T15:04:05+00:00",
      "last_error": null,
      "failures_total": 3,
      "consecutive_failures": 0,
      "last_duration_ms": 412
    },
    { "name": "proxmox", "state": "missing env", "detail": "PVE_URL, PVE_TOKEN_ID" },
    { "name": "astro", "state": "disabled", "detail": null }
  ],
  "ha": "connected | disconnected | unconfigured",
  "night": { "mode": "wake", "level": 100, "software": false, "method_used": "ddc" },
  "ws_clients": 2,
  "display_clients": 1,
  "dropped_messages": 0,
  "fixture": false,
  "revision": "03266d3ef0ab",
  "build": { "display": "/assets/display-….css|/assets/display-….js|…", "admin": "…" }
}
```

- `ok` means the backend process is alive — an upstream outage never makes it
  false. Collectors that stopped *attempting* polls (a hung fetch, an exited
  loop) are listed in `stuck`; that is what the watchdog restarts the backend
  for. Everything else worth a look is summarised in `problems`.
- Per collector: `overdue` = no fresh data for 3 intervals (+60 s);
  `degraded` = a partial failure the collector survives (some feeds, the sports
  schedule window, the hurricane outlook…); `state` is `running`, `dead`,
  `disabled`, `missing env` or `error` (its constructor rejected the config).
- `build` is the asset set of each built page (see "Deploys" under WebSockets).

### `GET /api/config` / `PUT /api/config`

The full config document (shape of `config/defaults.yaml`, as JSON). Stored
config is deep-merged over `defaults.yaml` on load and on save (dicts merge,
lists and scalars from the stored config win whole), so keys added to the
defaults later appear in existing databases too.

`PUT` validates first — structure (`rotation.interval_seconds ≥ 5`,
`poll_seconds*` ≥ 5, zero-padded `HH:MM` night times, levels 0–100…) and then
by constructing every enabled collector from the new config. On any problem it
answers `400 {"error": "…", "errors": [...]}` and **nothing is saved**. On
success it persists, restarts only the collectors whose inputs changed (their
module config, or the shared location for location-based modules), and
broadcasts `config` + `ha_states`. Reply: `{"ok": true, "restarted": [names]}`.

### `GET /api/config/history` · `GET /api/config/history/{id}` · `POST /api/config/restore`

Every save is kept (last 20). The list is newest first:
`{"versions": [{"id", "saved_at", "current", "changed": ["appearance.theme", …]}]}`.
`GET …/{id}` returns that version's document. `POST /api/config/restore
{"id": n}` applies it through the same validation as a `PUT` — and is itself
recorded, so a restore can be undone the same way.

### `POST /api/control`

```json
{ "action": "next" | "prev" | "pin" | "blank" | "wake" | "reload"
        | "celebrate_test" | "weather_alert_test" | "camera_alert_test" | "starship_test" }
```

Broadcast to all displays as a `control` message, except the `*_test` actions,
which broadcast the event they are testing instead: `celebrate_test` →
`sport_event` (a real cached play), `weather_alert_test` → `weather_alert` (a
canned Extreme alert), `camera_alert_test` → `camera_alert` (replays the first
alert configured for a takeover; `400` if none is). `starship_test` needs no
backend branch — it rides the generic `control` message and is handled entirely
by the display.

### `GET /api/cameras`

```json
{
  "cameras": [ { "id": "3f9a1c2b7e004d51", "entity_id": "camera.garage", "label": "garage" } ],
  "active_streams": 0, "max_streams": 6, "ha": "connected", "test_source": false
}
```

The proxyable set is derived from live config (`ha.cameras` plus any
`ha.alerts[].cameras`) — nothing else can be fetched. `active_streams` is the
teardown assertion: it must return to `0` shortly after a takeover ends.

### `GET /api/cameras/{id}/stream`

`multipart/x-mixed-replace` passthrough of HA's `/api/camera_proxy_stream/<entity>`,
for use as an `<img>` src. `400` bad id, `404` unknown id, `503` if HA is
unconfigured or all `max_streams` slots are busy, `502` if HA fails. The
display falls back to the snapshot route on any of these.

### `GET /api/cameras/{id}/snapshot`

Single JPEG from HA's `/api/camera_proxy/<entity>`, ~0.75 s TTL cache. Same
status codes. Fetched on takeover only — never polled at rest.

### `GET /api/ha/entities`

```json
{ "status": "connected", "entities": [ { "entity_id": "light.den", "domain": "light", "name": "Den", "state": "on" } ] }
```

### `POST /api/ha/action`

```json
{ "domain": "light", "service": "toggle", "entity_id": "light.den", "data": {} }
```

Only what the swipe-up overlay can do is allowed: the entity must be mapped in
`config.ha` (`scenes`, `lights`, `fans`, `climate`, `media` — alert entities are
state-only), and the service must be one of that group's
(`backend/ha_bridge.py` `CONTROL_SERVICES`, e.g. `light.toggle`,
`fan.set_percentage`, `climate.set_temperature`). Target keys inside `data`
(`entity_id`, `device_id`, `area_id`…) are dropped. `400` on missing
domain/service, `403` for anything outside the allowlist, `502` if HA is
unreachable. The WebSocket `ha_action` message follows the same rules.

## WebSockets

`/ws/display` and `/ws/admin` currently speak the same protocol; admin exists
as a separate channel so Phase 5 can add an admin-only health stream.

### Server → client

| `type` | Fields | Meaning |
|---|---|---|
| `snapshot` | `modules` (name → payload), `config`, `ha.status`, `ha.states`, `display_state`, `night`, `system.ip`, `build` | Full state on connect (`system.ip` = LAN address, `null` if unresolvable; refreshed per connect). `night` is the current dim state, so a display connecting mid-window (the 04:00 nightly reload lands in it) dims at once |
| `module` | `payload` | One module's latest payload |
| `module_removed` | `module` | A module's collector stopped (disabled): drop its payload and tape items |
| `config` | `config` | Config changed (re-apply rotation, HA mapping) |
| `control` | `action` | Remote control command |
| `night` | `mode` (`dim`\|`wake`), `level`, `software` | Sent on every brightness change. `software: true` = DDC/CI unavailable, the display draws the dim; `false` = the panel dims itself (clears any software overlay) |
| `display_state` | `state` | What the display is showing (admin live preview) |
| `ha_state` | `entity_id`, `state`, `attributes` | One mapped entity changed |
| `ha_states` | `status`, `states` | All mapped entity states (reconnect / remap) |
| `ha_status` | `status` | HA bridge connection status changed |
| `sport_event` | `event` | A followed team scored → celebration overlay |
| `fantasy_event` | `event` | A fantasy scoring play → celebration overlay |
| `weather_alert` | `alert` | Severe weather → full-screen takeover |
| `camera_alert` | `event` | Camera takeover (see below) |
| `pong` | — | Heartbeat reply |
| `error` | `error` | A client-initiated action failed |

### Client → server

| `type` | Fields | Meaning |
|---|---|---|
| `ping` | — | Heartbeat (display sends every 10 s) |
| `control` | `action` | Gesture-originated control |
| `ha_action` | `domain`, `service`, `entity_id`, `data` | Tile tap service call |
| `display_state` | `state` (`module`, `pinned`, `blanked`, `overlay`, `takeover`) | Display state report |

### Deploys: `build`

`build.display` / `build.admin` in the snapshot are the hashed assets each
page's built `index.html` links, sorted and `|`-joined. A production bundle
compares them with what its own document linked; the display reloads on a
mismatch (at most once per target build — `sessionStorage` guard), the admin
reloads if it has no unsaved edits. Every reconnect delivers a snapshot, so a
backend restart onto a new build moves the panel onto it without a `reload`
control message.

### Module payload

```json
{
  "module": "markets",
  "updated_at": "2026-06-11T15:04:05+00:00",
  "stale": false,
  "stage": { "...module-specific..." : "see collectors/*.py shape()" },
  "tape": [ { "text": "AAPL 213.40 ▲ 1.12%", "accent": "up", "priority": 0 } ]
}
```

`accent` ∈ `neutral | up | down | alert`. Higher `priority` sorts earlier
within a module's tape segment.

### `camera_alert` event

Source-agnostic full-screen camera takeover. Minted by `backend/camera_alert.py`
(`CameraAlertHub.fire()`), which any producer can call; the Home Assistant
bridge is the first consumer, firing on the **entry** edge of an alert that has
`takeover` set (leaving the state stays a toast).

```json
{
  "type": "camera_alert",
  "event": {
    "id": "9f31c0a4bd12",
    "key": "ha:binary_sensor.garage_door",
    "source": "home_assistant",
    "title": "Garage door open",
    "subtitle": "Garage Door",
    "severity": "alert",
    "transport": "stream",
    "cameras": [ { "id": "3f9a1c2b7e004d51", "label": "Garage" } ],
    "duration_seconds": 30,
    "wake": true,
    "issued_at": "2026-08-05T19:42:11.402+00:00"
  }
}
```

- `key` is stable per source object: it drives the per-key cooldown (60 s by
  default) and makes a re-fire mid-takeover *extend* the current one rather than
  queue a duplicate. `id` is per occurrence.
- `severity` ∈ `info | alert | critical` selects the entrance treatment.
- `transport`: `stream` uses the MJPEG proxy, `snapshot` forces ~1 Hz stills.
- `cameras[].id` is an **opaque proxy token**, never an entity_id or URL; the
  display builds `/api/cameras/<id>/stream` from it. 1–4 entries.
- `wake` is per event, not a display policy — a quiet alert sets it `false` and
  stays suppressed on a blanked panel.

Like `sport_event` and `weather_alert`, this is **not replayed** in the connect
snapshot: a display that reconnects a second later misses it.
