"""SQLite persistence: the live config document, seeded from config/defaults.yaml,
plus a short history of saved versions for one-click rollback."""
from __future__ import annotations

import hashlib
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
        await put_config(defaults())
    elif not await history():
        await _record(current)  # baseline for DBs that predate the history table


def defaults() -> dict:
    with open(DEFAULTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _merge(base[key], value) if key in base else value
        return out
    return override  # scalars and lists: the stored value wins whole


def with_defaults(config: dict) -> dict:
    """`config` over defaults.yaml: keys added to the defaults since the DB was
    seeded appear with their default values, everything stored wins.

    Existing databases never gained new default keys, so every collector and
    admin patch had to guard against a missing key — and some didn't (the
    admin couldn't toggle modules that postdate the DB, since its toggles
    iterate cfg.modules)."""
    return _merge(defaults(), config)


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def config_version(config) -> str:
    """Content hash of a config document. The admin sends it back in If-Match,
    so a save made from a stale copy is refused instead of silently reverting
    whatever another tab (or a restore) saved in between. Content, not a
    counter: two identical documents are the same version."""
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


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
