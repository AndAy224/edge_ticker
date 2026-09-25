"""WebRTC signalling relay: display <-> this backend <-> Home Assistant (go2rtc).

The camera takeover used to show HA's `camera_proxy_stream`, which for cameras
without a native MJPEG feed (UniFi Protect: all of them) is HA re-encoding a
snapshot every 0.5 s — a 2 fps slideshow at up to 15 Mbit/s per tile. HA's
built-in go2rtc serves the cameras' own H.264 over WebRTC instead: 20 fps at
~1-3 Mbit/s, measured from the kiosk's VLAN.

Only the signalling rides the backend (over the display's existing WebSocket
and the bridge's HA WebSocket), so the HA token never leaves the server; the
media flows HA -> Chromium directly. The same allowlist as the MJPEG proxy
applies: a display names a camera by its opaque token, and only cameras the
config selected for takeovers resolve.

For a wall of several tiles (`wall: true` on the offer) a camera's
`…_medium_resolution_channel` sibling is used when HA has it enabled: a tile
is ~850 px wide, and the 2688x1512 high-res channels (up to 10 Mbit/s, huge
keyframes) lost packets and stalled on the kiosk's Wi-Fi where the 1600x1200
doorbell never did. UniFi Protect ships those channels disabled in HA.

Protocol, per display connection (`request` is the display's own id):
  -> {type: webrtc_offer, request, camera, offer, wall} start a session
  -> {type: webrtc_candidate, request, candidate}     a local ICE candidate
  -> {type: webrtc_close, request}                    end it (also on disconnect)
  <- {type: webrtc, request, event}                   HA's session/answer/candidate/error
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .camera_alert import allowed_cameras

log = logging.getLogger(__name__)

# A takeover shows at most 4 tiles; this covers one refire overlapping the last.
MAX_SESSIONS = 8
HIGH_SUFFIX = "_high_resolution_channel"
MEDIUM_SUFFIX = "_medium_resolution_channel"


@dataclass
class _Session:
    owner: Any
    request: str
    entity_id: str
    send: Callable[[dict], None]
    sub_id: int | None = None
    session_id: str | None = None
    early_candidates: list[dict] = field(default_factory=list)
    closed: bool = False


class WebRTCRelay:
    def __init__(self, bridge, get_config: Callable[[], dict]) -> None:
        self.bridge = bridge
        self.get_config = get_config
        self._sessions: dict[tuple[int, str], _Session] = {}

    @property
    def active(self) -> int:
        return len(self._sessions)

    def _stream_entity(self, entity_id: str, wall: bool) -> str:
        if not wall or not entity_id.endswith(HIGH_SUFFIX):
            return entity_id
        medium = entity_id[: -len(HIGH_SUFFIX)] + MEDIUM_SUFFIX
        state = (getattr(self.bridge, "all_states", {}) or {}).get(medium) or {}
        if state.get("state") in (None, "unavailable", "unknown"):
            return entity_id
        return medium

    async def offer(
        self, owner, request: str, camera: str, sdp: str, send: Callable[[dict], None],
        wall: bool = False,
    ) -> None:
        def fail(message: str) -> None:
            send({"type": "webrtc", "request": request, "event": {"type": "error", "message": message}})

        if not isinstance(request, str) or not request or not isinstance(sdp, str) or not sdp:
            fail("malformed offer")
            return
        entity_id = allowed_cameras(self.get_config()).get(str(camera))
        if entity_id is None:
            fail("unknown camera")
            return
        # The allowlist is checked on the configured camera; its medium channel
        # is the same device.
        entity_id = self._stream_entity(entity_id, wall)
        if len(self._sessions) >= MAX_SESSIONS:
            fail("too many sessions")
            return
        key = (id(owner), request)
        if key in self._sessions:
            await self.close(owner, request)
        session = _Session(owner=owner, request=request, entity_id=entity_id, send=send)
        self._sessions[key] = session

        async def on_event(event: dict) -> None:
            if session.closed:
                return
            if event.get("type") == "session":
                session.session_id = event.get("session_id")
                pending, session.early_candidates = session.early_candidates, []
                for candidate in pending:
                    await self._forward_candidate(session, candidate)
            send({"type": "webrtc", "request": request, "event": event})

        try:
            session.sub_id = await self.bridge.subscribe(
                {"type": "camera/webrtc/offer", "entity_id": entity_id, "offer": sdp}, on_event
            )
        except Exception as exc:
            self._sessions.pop(key, None)
            log.info("webrtc offer %s: %s", entity_id, exc)
            fail(str(exc))
            return
        if session.closed:  # closed while the offer was in flight
            await self.bridge.unsubscribe(session.sub_id)

    async def candidate(self, owner, request: str, candidate: dict) -> None:
        session = self._sessions.get((id(owner), request))
        if session is None or not isinstance(candidate, dict):
            return
        if session.session_id is None:
            session.early_candidates.append(candidate)  # HA hasn't named the session yet
        else:
            await self._forward_candidate(session, candidate)

    async def _forward_candidate(self, session: _Session, candidate: dict) -> None:
        try:
            await self.bridge.send_command({
                "type": "camera/webrtc/candidate",
                "entity_id": session.entity_id,
                "session_id": session.session_id,
                "candidate": candidate,
            })
        except Exception as exc:
            log.info("webrtc candidate %s: %s", session.entity_id, exc)

    async def close(self, owner, request: str) -> None:
        session = self._sessions.pop((id(owner), request), None)
        if session is None:
            return
        session.closed = True
        if session.sub_id is not None:
            await self.bridge.unsubscribe(session.sub_id)

    async def close_owner(self, owner) -> None:
        """A display disconnected: end every session it had open."""
        for key in [k for k in self._sessions if k[0] == id(owner)]:
            await self.close(owner, key[1])
