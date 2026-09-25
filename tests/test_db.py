"""backend/db.py: seeding, with_defaults deep merge, config history."""
from __future__ import annotations

import json
import sqlite3

import pytest

from backend import db


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "ticker.db")
    await db.init()
    yield db
    await db.close()


async def test_init_seeds_defaults_and_a_history_baseline(fresh_db):
    assert await db.get_config() == db.defaults()
    history = await db.history()
    assert len(history) == 1
    assert history[0]["config"] == db.defaults()
    assert db.DB_PATH.exists()


async def test_put_config_round_trips_and_records_history(fresh_db):
    config = db.defaults()
    config["appearance"]["theme"] = "frost"
    await db.put_config(config)
    assert (await db.get_config())["appearance"]["theme"] == "frost"
    history = await db.history()
    assert len(history) == 2
    assert history[0]["config"]["appearance"]["theme"] == "frost"  # newest first
    assert history[0]["id"] > history[1]["id"]
    assert history[0]["saved_at"] >= history[1]["saved_at"]


async def test_history_keeps_last_twenty(fresh_db):
    for i in range(db.HISTORY_KEEP + 5):
        await db.put_config({"rotation": {"interval_seconds": 100 + i}})
    history = await db.history()
    assert len(history) == db.HISTORY_KEEP
    values = [h["config"]["rotation"]["interval_seconds"] for h in history]
    last = 100 + db.HISTORY_KEEP + 4
    assert values == list(range(last, last - db.HISTORY_KEEP, -1))


async def test_reinit_keeps_stored_config(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ticker.db")
    await db.init()
    await db.put_config({"rotation": {"order": ["news"]}})
    await db.close()
    await db.init()
    try:
        assert await db.get_config() == {"rotation": {"order": ["news"]}}
        assert len(await db.history()) == 2  # no extra baseline on a DB with history
    finally:
        await db.close()


async def test_db_predating_history_gets_a_baseline(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    stored = {"rotation": {"order": ["markets"]}}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO kv VALUES ('config', ?)", (json.dumps(stored),))
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init()
    try:
        assert await db.get_config() == stored
        history = await db.history()
        assert [h["config"] for h in history] == [stored]
    finally:
        await db.close()


async def test_calls_before_init_fail_loudly(monkeypatch):
    monkeypatch.setattr(db, "_conn", None)
    with pytest.raises(AssertionError):
        await db.get_config()
    with pytest.raises(AssertionError):
        await db.put_config({})


# ---- with_defaults --------------------------------------------------------------


def test_with_defaults_of_empty_is_defaults():
    assert db.with_defaults({}) == db.defaults()


def test_with_defaults_fills_keys_added_since_the_db_was_seeded():
    # A DB seeded before show_odds/win_probability/hurricanes existed.
    stored = {"modules": {"sports": {"enabled": False, "followed_teams": ["Rays"]}}}
    merged = db.with_defaults(stored)
    sports = merged["modules"]["sports"]
    assert sports["enabled"] is False  # stored wins
    assert sports["followed_teams"] == ["Rays"]  # lists replace whole, never append
    assert sports["show_odds"] is True  # new default key appears
    assert sports["leagues"] == db.defaults()["modules"]["sports"]["leagues"]
    assert "hurricanes" in merged["modules"]  # modules that postdate the DB appear


def test_with_defaults_stored_lists_and_scalars_win_whole():
    stored = {
        "rotation": {"order": []},
        "ha": {"climate": None, "lights": ["light.desk"]},
        "night": {"method": "software"},
    }
    merged = db.with_defaults(stored)
    assert merged["rotation"]["order"] == []
    assert merged["rotation"]["interval_seconds"] == 25
    assert merged["ha"]["climate"] is None
    assert merged["ha"]["lights"] == ["light.desk"]
    assert merged["night"]["method"] == "software"
    assert merged["night"]["dim_at"] == "23:00"


def test_with_defaults_keeps_unknown_stored_keys_and_type_overrides():
    stored = {"custom": {"x": 1}, "modules": {"news": {"feeds": [], "extra": True}}, "appearance": "odd"}
    merged = db.with_defaults(stored)
    assert merged["custom"] == {"x": 1}
    assert merged["modules"]["news"]["feeds"] == []
    assert merged["modules"]["news"]["extra"] is True
    assert merged["modules"]["news"]["keep"] == 30
    assert merged["appearance"] == "odd"  # a stored scalar replaces a default dict


def test_with_defaults_does_not_mutate_the_stored_config():
    stored = {"modules": {"sports": {"enabled": False}}}
    db.with_defaults(stored)
    assert stored == {"modules": {"sports": {"enabled": False}}}
