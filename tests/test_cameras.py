"""backend/api/cameras.py: the MJPEG proxy's stream slots. A slot is taken per
/stream request and has to come back on every way that request can end."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import JSONResponse

from backend.api import cameras
from backend.camera_alert import camera_token
from helpers import until

CONFIG = {"ha": {"cameras": ["camera.porch"]}}
CAM = camera_token("camera.porch")
BOUNDARY = "multipart/x-mixed-replace; boundary=ha-frame"
FRAMES = [b"--ha-frame\r\n\r\none\r\n", b"--ha-frame\r\n\r\ntwo\r\n"]


@pytest.fixture(autouse=True)
def _ha(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://ha.test:8123")
    monkeypatch.setenv("HA_TOKEN", "test-token")
    monkeypatch.setattr(cameras, "_active_streams", 0)


def request():
    state = SimpleNamespace(config=CONFIG)
    return SimpleNamespace(app=SimpleNamespace(state=state), url=SimpleNamespace(port=8081))


async def stream():
    return await asyncio.wait_for(cameras.camera_stream(CAM, request()), timeout=2)


def error_of(result) -> tuple[int, str]:
    assert isinstance(result, JSONResponse), result
    return result.status_code, json.loads(result.body)["error"]


async def silent(http_request: httpx.Request) -> httpx.Response:
    """Home Assistant accepted the connection and never answers."""
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


class Feed(httpx.AsyncByteStream):
    def __init__(self, frames=FRAMES) -> None:
        self.frames = list(frames)
        self.closed = False

    async def __aiter__(self):
        for frame in self.frames:
            yield frame

    async def aclose(self) -> None:
        self.closed = True


def serving(feed: Feed, status: int = 200):
    return lambda http_request: httpx.Response(
        status, headers={"content-type": BOUNDARY}, stream=feed
    )


async def serve(response, send) -> None:
    async def receive():
        await asyncio.Event().wait()

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)


# ---- before the response headers ---------------------------------------------------


async def test_an_upstream_that_never_sends_headers_gives_its_slot_back(http, monkeypatch):
    # read=None (needed between frames) also covered the wait for headers:
    # each such request held its slot for good, and after six of them every
    # stream was refused with 503 until the backend restarted.
    monkeypatch.setattr(cameras, "HEADER_TIMEOUT_SECONDS", 0.05)
    http.handler = silent
    for _ in range(cameras.MAX_CONCURRENT_STREAMS + 1):
        status, error = error_of(await stream())
        assert status == 502 and "no response within" in error
    assert cameras._active_streams == 0
    assert len(http.requests) == cameras.MAX_CONCURRENT_STREAMS + 1  # none was turned away


async def test_the_body_is_still_read_without_a_timeout(http):
    http.handler = silent
    task = asyncio.create_task(cameras.camera_stream(CAM, request()))
    await until(lambda: http.requests)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # frames can be seconds apart on a quiet camera
    assert http.client_kwargs[0]["timeout"].read is None


async def test_a_request_cancelled_while_waiting_gives_its_slot_back(http):
    http.handler = silent
    waiting = [
        asyncio.create_task(cameras.camera_stream(CAM, request()))
        for _ in range(cameras.MAX_CONCURRENT_STREAMS)
    ]
    await until(lambda: len(http.requests) == cameras.MAX_CONCURRENT_STREAMS)
    # waiting streams hold slots, and the count says so
    assert cameras._active_streams == cameras.MAX_CONCURRENT_STREAMS
    assert error_of(await stream()) == (503, "too many streams")

    for task in waiting:
        task.cancel()
    results = await asyncio.gather(*waiting, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    assert cameras._active_streams == 0
    http.handler = serving(Feed(), status=404)
    assert error_of(await stream())[0] == 502  # asked upstream again, not 503


async def test_an_upstream_error_status_is_a_502_and_closes_the_upstream(http):
    feed = Feed()
    http.handler = serving(feed, status=401)
    assert error_of(await stream()) == (502, "camera unavailable: upstream returned 401")
    assert feed.closed and cameras._active_streams == 0


async def test_too_many_streams_is_refused_without_asking_upstream(http, monkeypatch):
    monkeypatch.setattr(cameras, "_active_streams", cameras.MAX_CONCURRENT_STREAMS)
    assert error_of(await stream()) == (503, "too many streams")
    assert http.requests == []
    assert cameras._active_streams == cameras.MAX_CONCURRENT_STREAMS


# ---- once the stream is being served ------------------------------------------------


async def test_frames_pass_through_and_the_slot_is_freed_at_the_end(http):
    feed = Feed()
    http.handler = serving(feed)
    response = await stream()
    assert cameras._active_streams == 1
    assert http.requests[0].url == "http://ha.test:8123/api/camera_proxy_stream/camera.porch"
    assert http.requests[0].headers["authorization"] == "Bearer test-token"

    sent = []

    async def send(message):
        sent.append(message)

    await serve(response, send)
    assert dict(sent[0]["headers"])[b"content-type"] == BOUNDARY.encode()
    assert b"".join(m.get("body", b"") for m in sent[1:]) == b"".join(FRAMES)
    assert feed.closed and cameras._active_streams == 0


async def test_a_client_gone_before_the_first_byte_frees_the_slot(http):
    # The body generator is never started then, so its `finally` never runs.
    feed = Feed()
    http.handler = serving(feed)
    response = await stream()

    async def send(message):
        raise OSError("connection lost")

    with pytest.raises(Exception):
        await serve(response, send)
    assert feed.closed and cameras._active_streams == 0


async def test_a_client_gone_mid_stream_frees_the_slot(http):
    feed = Feed()
    http.handler = serving(feed)
    response = await stream()

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("connection lost")

    with pytest.raises(Exception):
        await serve(response, send)
    assert feed.closed and cameras._active_streams == 0


async def test_a_disconnect_cancelling_the_stream_frees_the_slot(http):
    feed = Feed()
    http.handler = serving(feed)
    response = await stream()
    stalled = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            stalled.set()
            await asyncio.Event().wait()  # a client that stopped reading

    task = asyncio.create_task(serve(response, send))
    await asyncio.wait_for(stalled.wait(), timeout=2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert feed.closed and cameras._active_streams == 0
