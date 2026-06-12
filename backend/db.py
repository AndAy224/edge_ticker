"""SQLite persistence: the live config document, seeded from config/defaults.yaml."""
from __future__ import annotations

import json
import os
from pathlib import Path

import aiosqlite
import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("TICKER_DB", ROOT / "data" / "ticker.db"))
DEFAULTS_PATH = ROOT / "config" / "defaults.yaml"

_conn: aiosqlite.Connection | None = None


async def init() -> None:
    global _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(DB_PATH)
    await _conn.execute(
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    await _conn.commit()
    if await get_config() is None:
        with open(DEFAULTS_PATH, encoding="utf-8") as f:
            seed = yaml.safe_load(f)
        await put_config(seed)


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def get_config() -> dict | None:
    assert _conn is not None, "db.init() not called"
    async with _conn.execute("SELECT value FROM kv WHERE key = 'config'") as cursor:
        row = await cursor.fetchone()
    return json.loads(row[0]) if row else None


async def put_config(config: dict) -> None:
    assert _conn is not None, "db.init() not called"
    await _conn.execute(
        "INSERT OR REPLACE INTO kv (key, value) VALUES ('config', ?)",
        (json.dumps(config),),
    )
    await _conn.commit()
