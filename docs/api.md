# edge-ticker — API reference

Single backend on port 8080 serves both frontends, the REST API, and both
WebSocket channels.

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
  "collectors": [
    {
      "name": "markets",
      "interval": 60,
      "stale": false,
      "last_success": "2026-06-11T15:04:05+00:00",
      "last_error": null
    }
  ],
  "ha": "connected | disconnected | unconfigured",
  "ws_clients": 2
}
```

### `GET /api/config` / `PUT /api/config`

The full config document (shape of `config/defaults.yaml`, as JSON). `PUT`
persists to SQLite, restarts collectors, and broadcasts `config` +
`ha_states` messages to all WS clients.

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

`400` on missing domain/service, `502` if HA is unreachable.

## WebSockets

`/ws/display` and `/ws/admin` currently speak the same protocol; admin exists
as a separate channel so Phase 5 can add an admin-only health stream.

### Server → client

| `type` | Fields | Meaning |
|---|---|---|
| `snapshot` | `modules` (name → payload), `config`, `ha.status`, `ha.states`, `display_state`, `system.ip` | Full state on connect (`system.ip` = LAN address, `null` if unresolvable; refreshed per connect) |
| `module` | `payload` | One module's latest payload |
| `config` | `config` | Config changed (re-apply rotation, HA mapping) |
| `control` | `action` | Remote control command |
| `night` | `mode` (`dim`\|`wake`), `level` | Software dim fallback (DDC/CI unavailable) |
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
