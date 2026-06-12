"""Dynamic collector discovery — adding a module never touches core files.

Any Collector subclass defined in a module of this package is picked up
automatically and instantiated if config enables it (default: enabled).
"""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil

from .base import Collector

log = logging.getLogger(__name__)


def discover_collectors(config: dict) -> list[Collector]:
    collectors: list[Collector] = []
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
                module_config = config.get("modules", {}).get(cls.name, {})
                if module_config.get("enabled", True):
                    collectors.append(cls(config))
    return collectors
