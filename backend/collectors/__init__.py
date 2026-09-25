"""Dynamic collector discovery — adding a module never touches core files.

Any Collector subclass defined in a module of this package is picked up
automatically and instantiated if config enables it (default: enabled).
"""
from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
from dataclasses import dataclass, field

from .base import Collector

log = logging.getLogger(__name__)

_classes: list[type[Collector]] | None = None


def collector_classes() -> list[type[Collector]]:
    """Every Collector subclass in this package (imported once, then cached)."""
    global _classes
    if _classes is not None:
        return _classes
    found: list[type[Collector]] = []
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod_info.name}")
        except Exception as exc:
            log.error("failed to import collector module %s: %s", mod_info.name, exc)
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, Collector)
                and cls is not Collector
                and cls.__module__ == module.__name__
            ):
                found.append(cls)
    _classes = found
    return found


@dataclass
class Discovery:
    collectors: list[Collector] = field(default_factory=list)
    # name -> why it isn't running: "disabled", "missing env: X", "error: …"
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> dict[str, str]:
        return {n: r for n, r in self.skipped.items() if r.startswith("error:")}


def discover_collectors(config: dict) -> Discovery:
    """Build the collectors `config` enables. A collector whose constructor
    rejects its config is skipped and reported, never raised: one bad value
    (`days_back: "four"`) used to stop every collector, and — the config being
    saved by then — crash-loop the next boot."""
    found = Discovery()
    for cls in collector_classes():
        module_config = (config.get("modules") or {}).get(cls.name, {})
        if not isinstance(module_config, dict):
            found.skipped[cls.name] = "error: module config must be an object"
            continue
        if not module_config.get("enabled", cls.enabled_by_default):
            found.skipped[cls.name] = "disabled"
            continue
        missing = [e for e in cls.required_env if not os.environ.get(e)]
        if missing:
            log.info("skipping collector %s (missing env: %s)", cls.name, ", ".join(missing))
            found.skipped[cls.name] = f"missing env: {', '.join(missing)}"
            continue
        try:
            found.collectors.append(cls(config))
        except Exception as exc:
            log.error("collector %s rejected its config: %s", cls.name, exc)
            found.skipped[cls.name] = f"error: {type(exc).__name__}: {exc}"
    return found
