"""SQLite persistence: the live config document, seeded from config/defaults.yaml,
plus a short history of saved versions for one-click rollback."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("TICKER_DB", ROOT / "data" / "ticker.db"))
DEFAULTS_PATH = ROOT / "config" / "defaults.yaml"
HISTORY_KEEP = 20

_conn: aiosqlite.Connection | None = None


async def init() -> None:
    global _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(DB_PATH)
    await _conn.execute(
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    await _conn.execute(
        "CREATE TABLE IF NOT EXISTS config_history ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " saved_at TEXT NOT NULL,"
        " value TEXT NOT NULL)"
    )
    await _conn.commit()
    current = await get_config()
    if current is None:
        with open(DEFAULTS_PATH, encoding="utf-8") as f:
            seed = yaml.safe_load(f)
        await put_config(seed)
    elif not await history():
        await _record(current)  # baseline for DBs that predate the history table


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
    await _record(config, commit=False)
    await _conn.commit()


async def _record(config: dict, commit: bool = True) -> None:
    assert _conn is not None
    await _conn.execute(
        "INSERT INTO config_history (saved_at, value) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), json.dumps(config)),
    )
    await _conn.execute(
        "DELETE FROM config_history WHERE id NOT IN"
        " (SELECT id FROM config_history ORDER BY id DESC LIMIT ?)",
        (HISTORY_KEEP,),
    )
    if commit:
        await _conn.commit()


async def history() -> list[dict]:
    """Saved versions, newest first: [{id, saved_at, config}]."""
    assert _conn is not None, "db.init() not called"
    async with _conn.execute(
        "SELECT id, saved_at, value FROM config_history ORDER BY id DESC"
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"id": r[0], "saved_at": r[1], "config": json.loads(r[2])} for r in rows]
