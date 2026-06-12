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
{ "action": "next" | "prev" | "pin" | "blank" | "wake" | "reload" }
```

Broadcast to all displays as a `control` message.

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
| `snapshot` | `modules` (name → payload), `config`, `ha.status`, `ha.states`, `display_state` | Full state on connect |
| `module` | `payload` | One module's latest payload |
| `config` | `config` | Config changed (re-apply rotation, HA mapping) |
| `control` | `action` | Remote control command |
| `night` | `mode` (`dim`\|`wake`), `level` | Software dim fallback (DDC/CI unavailable) |
| `display_state` | `state` | What the display is showing (admin live preview) |
| `ha_state` | `entity_id`, `state`, `attributes` | One mapped entity changed |
| `ha_states` | `status`, `states` | All mapped entity states (reconnect / remap) |
| `ha_status` | `status` | HA bridge connection status changed |
| `pong` | — | Heartbeat reply |
| `error` | `error` | A client-initiated action failed |

### Client → server

| `type` | Fields | Meaning |
|---|---|---|
| `ping` | — | Heartbeat (display sends every 10 s) |
| `control` | `action` | Gesture-originated control |
| `ha_action` | `domain`, `service`, `entity_id`, `data` | Tile tap service call |
| `display_state` | `state` (`module`, `pinned`, `blanked`, `overlay`) | Display state report |

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
