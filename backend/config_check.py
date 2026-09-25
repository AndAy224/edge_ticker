"""Validate a config document before it is saved.

Saving first and discovering the problem later is how a single bad value
bricked the appliance: the collectors died on the spot, and because the config
was already persisted the next boot crash-looped with the admin GUI down too.
So a PUT is checked structurally, then every collector it enables is actually
constructed from it — the same code path startup runs.
"""
from __future__ import annotations

import re
from typing import Any

from .collectors import discover_collectors
from .collectors.base import MIN_INTERVAL_SECONDS

# Zero-padded 24h: the night scheduler compares these as strings. Used with
# fullmatch — `$` alone would also accept "04:00\n", which never equals a minute.
HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
MIN_ROTATION_SECONDS = 5


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _object(config: dict, key: str, errors: list[str]) -> dict:
    value = config.get(key, {})
    if not isinstance(value, dict):  # null included: every consumer .get()s it
        errors.append(f"{key} must be an object")
        return {}
    return value


def validate(config: Any) -> list[str]:
    """Human-readable problems with `config`; empty when it is safe to save."""
    if not isinstance(config, dict):
        return ["config must be a JSON object"]
    errors: list[str] = []

    rotation = _object(config, "rotation", errors)
    order = rotation.get("order", [])
    if not isinstance(order, list) or not all(isinstance(m, str) for m in order):
        errors.append("rotation.order must be a list of module ids")
    interval = rotation.get("interval_seconds", 25)
    if not _number(interval) or interval < MIN_ROTATION_SECONDS:
        errors.append(f"rotation.interval_seconds must be a number ≥ {MIN_ROTATION_SECONDS}")

    modules = _object(config, "modules", errors)
    for name, module in modules.items():
        if not isinstance(module, dict):
            errors.append(f"modules.{name} must be an object")
            continue
        for key, value in module.items():
            if key.startswith("poll_seconds") and value is not None:
                if not _number(value) or value < MIN_INTERVAL_SECONDS:
                    errors.append(
                        f"modules.{name}.{key} must be a number ≥ {MIN_INTERVAL_SECONDS:g}"
                    )

    night = _object(config, "night", errors)
    for key in ("dim_at", "wake_at", "nightly_reload_at"):
        value = night.get(key)
        if value not in (None, "") and not (isinstance(value, str) and HHMM.fullmatch(value)):
            errors.append(f"night.{key} must be HH:MM (24-hour, zero-padded)")
    for key in ("dim_level", "day_level"):
        # null too: the scheduler does int() on these every minute
        if key in night and (not _number(night[key]) or not 0 <= night[key] <= 100):
            errors.append(f"night.{key} must be a number from 0 to 100")
    if night.get("method", "ddc") not in ("ddc", "software"):
        errors.append("night.method must be ddc or software")

    _object(config, "appearance", errors)
    _object(config, "ha", errors)

    if not errors:  # constructing collectors from a malformed shape just repeats the above
        for name, reason in discover_collectors(config).errors.items():
            errors.append(f"modules.{name}: {reason.removeprefix('error: ')}")
    return errors


def changed_paths(old: Any, new: Any, prefix: str = "", limit: int = 8) -> list[str]:
    """Dotted paths that differ between two config documents (lists compare
    whole). Feeds the admin's config history — "what did that save change?"."""
    out: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if len(out) >= limit or a == b:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b), key=str):
                walk(a.get(key), b.get(key), f"{path}.{key}" if path else str(key))
        else:
            out.append(path or "(root)")

    walk(old, new, prefix)
    return out
