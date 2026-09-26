"""backend/collectors/__init__.py: collector discovery and skip reasons."""
from __future__ import annotations

from backend.collectors import Discovery, collector_classes, discover_collectors

# Modules known when this suite was written; new ones may be added freely.
KNOWN_MODULES = {
    "adsb", "airquality", "astro", "fantasy", "hurricanes", "launches", "markets",
    "news", "opnsense", "proxmox", "sports", "weather", "weather_alerts", "weather_radar",
}
PVE_ENV = {"PVE_URL": "https://pve.invalid:8006", "PVE_TOKEN_ID": "t@pam!x", "PVE_TOKEN_SECRET": "s"}


def names(found: Discovery) -> set[str]:
    return {c.name for c in found.collectors}


def test_every_collector_module_is_discovered():
    classes = collector_classes()
    discovered = [cls.name for cls in classes]
    assert KNOWN_MODULES <= set(discovered)
    assert len(discovered) == len(set(discovered))  # module names are unique
    assert collector_classes() is classes  # imported once, then cached


def test_shipped_defaults(defaults_config):
    found = discover_collectors(defaults_config)
    assert found.errors == {}
    assert set(found.skipped.values()) == {"disabled"}  # nothing needs env or errors
    for stretch in ("proxmox", "adsb", "astro", "opnsense"):
        assert found.skipped[stretch] == "disabled"
    v1 = {"markets", "sports", "news", "weather", "weather_alerts", "weather_radar",
          "hurricanes", "launches", "airquality", "fantasy"}
    assert v1 <= names(found)


def test_enabled_by_default_applies_when_module_config_is_absent():
    found = discover_collectors({"modules": {}})
    assert "weather" in names(found)
    for stretch in ("adsb", "astro", "proxmox"):
        assert found.skipped[stretch] == "disabled"


def test_missing_env_is_a_skip_not_an_error(defaults_config):
    defaults_config["modules"]["proxmox"]["enabled"] = True
    found = discover_collectors(defaults_config)
    assert found.skipped["proxmox"] == "missing env: PVE_URL, PVE_TOKEN_ID, PVE_TOKEN_SECRET"
    assert "proxmox" not in found.errors


def test_required_env_present_constructs(defaults_config, monkeypatch):
    for key, value in PVE_ENV.items():
        monkeypatch.setenv(key, value)
    defaults_config["modules"]["proxmox"]["enabled"] = True
    found = discover_collectors(defaults_config)
    assert "proxmox" in names(found)
    assert "proxmox" not in found.skipped


def test_partial_env_lists_only_what_is_missing(defaults_config, monkeypatch):
    monkeypatch.setenv("PVE_URL", PVE_ENV["PVE_URL"])
    defaults_config["modules"]["proxmox"]["enabled"] = True
    found = discover_collectors(defaults_config)
    assert found.skipped["proxmox"] == "missing env: PVE_TOKEN_ID, PVE_TOKEN_SECRET"


def test_constructor_errors_skip_only_that_collector(defaults_config):
    defaults_config["modules"]["sports"]["max_recent"] = "six"
    found = discover_collectors(defaults_config)
    assert found.skipped["sports"].startswith("error: ValueError:")
    assert found.errors == {"sports": found.skipped["sports"]}
    assert "news" in names(found) and "sports" not in names(found)


def test_non_object_module_config_is_an_error(defaults_config):
    defaults_config["modules"]["news"] = "rss"
    found = discover_collectors(defaults_config)
    assert found.skipped["news"] == "error: module config must be an object"
    assert "news" in found.errors


def test_collectors_get_the_whole_config(defaults_config):
    found = discover_collectors(defaults_config)
    radar = next(c for c in found.collectors if c.name == "weather_radar")
    assert radar.config is defaults_config
    assert radar.module_config is defaults_config["modules"]["weather_radar"]
    assert (radar.latitude, radar.longitude) == (27.9659, -82.8001)
