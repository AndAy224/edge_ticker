"""Home Assistant camera proxy for the full-screen takeover overlay.

The display asks for `/api/cameras/<opaque id>/stream` and gets HA's MJPEG feed
passed through byte for byte; if that fails it falls back to
`/api/cameras/<id>/snapshot`, a single JPEG with a short TTL cache. Same rules
as the fantasy logo proxy (api/fantasy.py): the client passes a validated
opaque id and never a URL, the upstream host is a server-side constant, the HA
token never leaves the backend, and every failure is a JSON status code rather
than an exception.

Fetched on takeover only — never polled at rest.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..camera_alert import allowed_cameras

log = logging.getLogger(__name__)

router = APIRouter()

CAM_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Chromium allows six connections per host; three tiles plus the display
# WebSocket fits comfortably, and this covers a second panel. Past the cap we
# fail fast so the <img> errors and the tile degrades to snapshots, rather than
# queueing behind an in-flight stream.
MAX_CONCURRENT_STREAMS = 6
# Slots in use: one per /stream request from before it connects to HA until its
# teardown has run. The cap and the number /api/cameras reports are this one
# counter — it used to count only streams whose headers had arrived, beside a
# semaphore that could be held (and leaked) with the count still reading 0.
_active_streams = 0

# How long HA gets to answer with response headers. The client's read timeout
# has to stay off for the body (see camera_stream), and it covered this wait
# too: an HA that accepted the connection and then said nothing held the slot
# for good, and six of those refused every stream until a restart.
HEADER_TIMEOUT_SECONDS = 10.0

SNAPSHOT_TTL_SECONDS = 0.75  # tiles poll at ~1 Hz; don't stampede HA
SNAPSHOT_MAX_ENTRIES = 8
_snap_cache: dict[str, tuple[float, str, bytes]] = {}  # id -> (ts, content_type, bytes)

# Dev-only fake feeds. The dev .env points HA_URL at homeassistant.local, which
# does not resolve off the appliance, so without this there is nothing to point
# the overlay at on :8081.
TEST_SOURCE = os.environ.get("CAMERA_TEST_SOURCE") == "1"
TEST_IDS = ("__test0", "__test1", "__test2", "__test3")
TEST_COLORS = ("#1b4d6b", "#6b3a1b", "#1b6b3a", "#4d1b6b")
TEST_FRAME_INTERVAL = 0.2


def _ha_env() -> tuple[str, str]:
    return os.environ.get("HA_URL", "").rstrip("/"), os.environ.get("HA_TOKEN", "")


def _resolve(config: dict, cam_id: str) -> str | None:
    return allowed_cameras(config).get(cam_id)


@router.get("/cameras")
async def cameras_index(request: Request):
    """Proxyable cameras plus live stream accounting.

    `active_streams` is the teardown assertion: it must return to 0 shortly
    after a takeover dismisses, or the display is leaking MJPEG connections.
    """
    mapping = allowed_cameras(request.app.state.config)
    return {
        "cameras": [
            {"id": cam_id, "entity_id": eid, "label": eid.split(".", 1)[-1]}
            for cam_id, eid in sorted(mapping.items(), key=lambda kv: kv[1])
        ],
        "active_streams": _active_streams,
        "max_streams": MAX_CONCURRENT_STREAMS,
        # WebRTC sessions being relayed (backend/webrtc.py); the same teardown
        # assertion applies — back to 0 after a takeover.
        "active_webrtc": request.app.state.webrtc.active,
        "ha": getattr(request.app.state.ha, "status", "unknown"),
        "test_source": TEST_SOURCE,
    }


# ---- test source -----------------------------------------------------------


def _test_index(cam_id: str) -> int | None:
    return TEST_IDS.index(cam_id) if TEST_SOURCE and cam_id in TEST_IDS else None


def _test_svg(index: int, frame: int = 0) -> bytes:
    """A readable stand-in frame: <img> renders SVG and honors object-fit.

    The moving bar makes "is this actually live?" answerable from two
    screenshots, which a static test pattern cannot do.
    """
    stamp = time.strftime("%H:%M:%S")
    x = 6 + (frame * 4) % 76
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540">'
        f'<rect width="100%" height="100%" fill="{TEST_COLORS[index]}"/>'
        f'<rect x="{x}%" y="8%" width="14%" height="10%" fill="#ffffff" opacity="0.85"/>'
        f'<text x="50%" y="48%" fill="#fff" font-size="72" font-family="monospace" '
        f'text-anchor="middle">TEST CAM {index}</text>'
        f'<text x="50%" y="66%" fill="#fff" font-size="48" font-family="monospace" '
        f'text-anchor="middle">{stamp} · {frame:03d}</text>'
        f"</svg>"
    ).encode()


@router.get("/cameras/{cam_id}/mjpeg")
async def camera_test_mjpeg(cam_id: str):
    """A real multipart/x-mixed-replace stream, so /stream has a live upstream
    to proxy on the dev box — the proxy lifecycle is what we actually want to
    exercise here, not a shortcut around it."""
    index = _test_index(cam_id)
    if index is None:
        return JSONResponse({"error": "test source disabled"}, status_code=404)

    async def frames():
        n = 0
        while True:
            part = _test_svg(index, n)
            yield (
                b"--frame\r\nContent-Type: image/svg+xml\r\n"
                b"Content-Length: " + str(len(part)).encode() + b"\r\n\r\n"
                + part
                + b"\r\n"
            )
            n += 1
            await asyncio.sleep(TEST_FRAME_INTERVAL)

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


# ---- stream ----------------------------------------------------------------


def _take_slot() -> bool:
    global _active_streams
    if _active_streams >= MAX_CONCURRENT_STREAMS:
        return False
    _active_streams += 1
    return True


def _free_slot() -> None:
    global _active_streams
    _active_streams -= 1


class _ProxiedStream(StreamingResponse):
    """A StreamingResponse that runs `release` once it is done being served.

    The body generator's own `finally` only runs if the generator was started
    and then finished or was cancelled: a response whose first send fails never
    starts it, and one whose send fails mid-stream leaves it parked at a yield
    until it is collected."""

    def __init__(self, *args, release, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._release = release

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._release()


@router.get("/cameras/{cam_id}/stream")
async def camera_stream(cam_id: str, request: Request):
    test_index = _test_index(cam_id)
    if test_index is None:
        if not CAM_ID_RE.match(cam_id):
            return JSONResponse({"error": "bad camera id"}, status_code=400)
        entity_id = _resolve(request.app.state.config, cam_id)
        if entity_id is None:
            return JSONResponse({"error": "unknown camera"}, status_code=404)
        base, token = _ha_env()
        if not base or not token:
            return JSONResponse(
                {"error": "home assistant not configured"}, status_code=503
            )
        url = f"{base}/api/camera_proxy_stream/{entity_id}"
        headers = {"Authorization": f"Bearer {token}"}
    else:
        entity_id = cam_id
        port = request.url.port or 8080
        url = f"http://127.0.0.1:{port}/api/cameras/{cam_id}/mjpeg"
        headers = {}

    if not _take_slot():
        return JSONResponse({"error": "too many streams"}, status_code=503)

    # read=None is load-bearing: MJPEG frames can be seconds apart on a quiet
    # camera, and any read timeout would kill a perfectly healthy feed.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        follow_redirects=True,
    )
    upstream: httpx.Response | None = None
    released = False

    async def release() -> None:
        """Close the upstream and the client and give the slot back, once.
        The slot is freed even if a close is cancelled or raises."""
        nonlocal released
        if released:
            return
        released = True
        try:
            if upstream is not None:
                await upstream.aclose()
        finally:
            try:
                await client.aclose()
            finally:
                _free_slot()

    try:
        # wait_for bounds only the wait for headers; the body below is read
        # with no timeout between frames.
        upstream = await asyncio.wait_for(
            client.send(client.build_request("GET", url, headers=headers), stream=True),
            timeout=HEADER_TIMEOUT_SECONDS,
        )
        if upstream.status_code != 200:
            raise RuntimeError(f"upstream returned {upstream.status_code}")
    except BaseException as exc:
        # BaseException: a request cancelled while it waits here (the client
        # went away, shutdown) skipped an `except Exception` and kept its slot.
        await release()
        if not isinstance(exc, Exception):
            raise
        reason = (
            f"no response within {HEADER_TIMEOUT_SECONDS:g} s"
            if isinstance(exc, asyncio.TimeoutError)
            else str(exc)
        )
        log.warning("camera stream %s: %s", entity_id, reason)
        return JSONResponse({"error": f"camera unavailable: {reason}"}, status_code=502)

    async def body():
        # The client and the upstream response outlive the handler — an
        # `async with` around it would close them before Starlette pulled the
        # first chunk.
        try:
            # aiter_raw, not aiter_bytes: pass the multipart body through
            # untouched, with no content decoding and no extra buffering.
            async for chunk in upstream.aiter_raw():
                yield chunk
        except Exception as exc:
            log.info("camera stream %s ended: %s", entity_id, exc)
        finally:
            # Upstream EOF, upstream error, and client disconnect where
            # Starlette cancels the generator (which raises inside the async
            # for). _ProxiedStream covers the exits that never get here.
            await release()

    return _ProxiedStream(
        body(),
        release=release,
        # Passed through verbatim — it carries the multipart boundary, and
        # inventing one breaks the parse.
        media_type=upstream.headers.get(
            "content-type", "multipart/x-mixed-replace; boundary=frame"
        ),
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )


# ---- snapshot --------------------------------------------------------------


@router.get("/cameras/{cam_id}/snapshot")
async def camera_snapshot(cam_id: str, request: Request):
    test_index = _test_index(cam_id)
    if test_index is not None:
        return Response(content=_test_svg(test_index), media_type="image/svg+xml")
    if not CAM_ID_RE.match(cam_id):
        return JSONResponse({"error": "bad camera id"}, status_code=400)
    entity_id = _resolve(request.app.state.config, cam_id)
    if entity_id is None:
        return JSONResponse({"error": "unknown camera"}, status_code=404)
    base, token = _ha_env()
    if not base or not token:
        return JSONResponse({"error": "home assistant not configured"}, status_code=503)

    now = time.monotonic()
    cached = _snap_cache.get(cam_id)
    if cached and now - cached[0] < SNAPSHOT_TTL_SECONDS:
        return Response(content=cached[2], media_type=cached[1])

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(
                f"{base}/api/camera_proxy/{entity_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
    except Exception as exc:
        log.info("camera snapshot %s: %s", entity_id, exc)
        return JSONResponse({"error": f"snapshot failed: {exc}"}, status_code=502)

    content_type = r.headers.get("content-type", "image/jpeg").split(";")[0]
    data = r.content
    if len(_snap_cache) >= SNAPSHOT_MAX_ENTRIES:
        _snap_cache.pop(min(_snap_cache, key=lambda k: _snap_cache[k][0]))
    _snap_cache[cam_id] = (now, content_type, data)
    return Response(
        content=data, media_type=content_type, headers={"Cache-Control": "no-store"}
    )
