"""Importable test helpers (conftest.py holds the fixtures that use them)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def json_response(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


class FakeBus:
    """Records broadcasts; enough of Bus for collectors and the scheduler."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.payloads: dict = {}

    async def broadcast(self, message: dict) -> None:
        self.messages.append(message)

    async def publish(self, payload) -> None:
        self.payloads[payload.module] = payload
        self.messages.append({"type": "module", "payload": payload})

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m.get("type") == kind]


class HttpMock:
    """Routes every httpx.AsyncClient a collector builds to a MockTransport.

    Set `.handler` to a function (httpx.Request) -> httpx.Response. Requests
    and the kwargs each client was constructed with are recorded."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict] = []
        self.handler = lambda request: httpx.Response(404)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


async def until(predicate, timeout: float = 2.0) -> None:
    """Yield to the loop until predicate() holds (collector tasks run meanwhile)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
