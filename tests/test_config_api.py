"""PUT /api/config's optimistic concurrency: ETag on read, If-Match on save."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from backend import db
from backend.api.config import router
from helpers import FakeBus


class FakeManager:
    async def apply(self, bus, config):
        return []


class FakeBridge:
    status = "unconfigured"

    def mapped_states(self):
        return {}


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ticker.db")
    await db.init()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.config = db.with_defaults(await db.get_config())
    app.state.bus = FakeBus()
    app.state.manager = FakeManager()
    app.state.ha = FakeBridge()
    yield app
    await db.close()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ticker") as c:
        yield c


def themed(config: dict, theme: str) -> dict:
    out = {**config, "appearance": {**config["appearance"], "theme": theme}}
    return out


async def test_get_carries_the_version(client, app):
    r = await client.get("/api/config")
    assert r.headers["etag"] == f'"{db.config_version(app.state.config)}"'


async def test_a_save_from_the_current_version_goes_through(client, app):
    r = await client.get("/api/config")
    put = await client.put("/api/config", json=themed(r.json(), "frost"),
                           headers={"If-Match": r.headers["etag"]})
    assert put.status_code == 200
    reply = put.json()
    assert reply["config"]["appearance"]["theme"] == "frost"
    assert reply["version"] == db.config_version(app.state.config)
    assert put.headers["etag"] == f'"{reply["version"]}"'
    (msg,) = app.state.bus.of_type("config")
    assert msg["version"] == reply["version"]


async def test_a_save_from_a_stale_copy_is_refused(client, app):
    first = await client.get("/api/config")
    # another tab saves in between
    other = await client.put("/api/config", json=themed(first.json(), "amber"),
                             headers={"If-Match": first.headers["etag"]})
    assert other.status_code == 200
    stale = await client.put("/api/config", json=themed(first.json(), "frost"),
                             headers={"If-Match": first.headers["etag"]})
    assert stale.status_code == 409
    reply = stale.json()
    assert reply["config"]["appearance"]["theme"] == "amber"  # theirs, untouched
    assert reply["version"] == other.json()["version"]
    assert app.state.config["appearance"]["theme"] == "amber"
    assert len(app.state.bus.of_type("config")) == 1


async def test_a_save_without_if_match_still_overwrites(client, app):
    # curl and the layout audit don't send it
    r = await client.get("/api/config")
    put = await client.put("/api/config", json=themed(r.json(), "glance"))
    assert put.status_code == 200
    assert app.state.config["appearance"]["theme"] == "glance"


async def test_the_version_is_content_not_key_order():
    a = {"b": 1, "a": {"y": [1, 2], "x": None}}
    b = {"a": {"x": None, "y": [1, 2]}, "b": 1}
    assert db.config_version(a) == db.config_version(b)
    assert db.config_version(a) != db.config_version({**a, "b": 2})
