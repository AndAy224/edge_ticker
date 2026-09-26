"""collectors/opnsense.py: which WAN is live, rates from counters, VLAN parents, tape."""
from __future__ import annotations

import base64

import httpx
import pytest

from backend.collectors.opnsense import OpnsenseCollector, _uptime, fmt_rate
from helpers import json_response, load_json

ENDPOINT_FIXTURES = {
    "/api/routes/gateway/status": "opnsense_gateway_status.json",
    "/api/routing/settings/searchGateway": "opnsense_search_gateway.json",
    "/api/diagnostics/interface/getRoutes": "opnsense_routes.json",
    "/api/diagnostics/traffic/interface": "opnsense_traffic.json",
    "/api/interfaces/overview/interfacesInfo": "opnsense_interfaces_info.json",
    "/api/diagnostics/system/systemTime": "opnsense_system_time.json",
    "/api/diagnostics/system/systemResources": "opnsense_system_resources.json",
    "/api/diagnostics/firewall/pf_states": "opnsense_pf_states.json",
    "/api/diagnostics/system/systemInformation": "opnsense_system_information.json",
}


@pytest.fixture
def opn_env(monkeypatch):
    monkeypatch.setenv("OPNSENSE_URL", "http://opnsense.invalid/")
    monkeypatch.setenv("OPNSENSE_KEY", "the-key")
    monkeypatch.setenv("OPNSENSE_SECRET", "the-secret")


@pytest.fixture
def collector(opn_env, defaults_config):
    return OpnsenseCollector(defaults_config)


def recorded_handler(failing: set[str] = frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in failing:
            return httpx.Response(500)
        return json_response(load_json(ENDPOINT_FIXTURES[request.url.path]))

    return handler


def raw(traffic: str = "opnsense_traffic.json") -> dict:
    """What fetch() returns for the recorded firewall (WAN up, Starlink unplugged)."""
    return {
        "gateway_status": load_json("opnsense_gateway_status.json"),
        "gateways": load_json("opnsense_search_gateway.json"),
        "routes": load_json("opnsense_routes.json"),
        "traffic": load_json(traffic),
        "interfaces": load_json("opnsense_interfaces_info.json"),
        "health": {
            "system_time": load_json("opnsense_system_time.json"),
            "resources": load_json("opnsense_system_resources.json"),
            "states": load_json("opnsense_pf_states.json"),
        },
        "version": "26.7.4_1",
    }


def failover_raw(route_via: str = "igc0") -> dict:
    """The primary's gateway dead, Starlink linked and online."""
    data = raw()
    for gw in data["gateway_status"]["items"]:
        if gw["name"] == "WAN_GW":
            gw.update(status="down", status_translated="Offline", loss="100.0 %", delay="~")
        if gw["name"] == "STARLINK_DHCP6":
            gw.update(status="none", status_translated="Online", address="100.64.0.1",
                      delay="31.5 ms", loss="0.0 %", stddev="4.2 ms")
    for row in data["interfaces"]["rows"]:
        if row["device"] == "igc0":
            row.update(status="up", addr4="100.64.12.34/10")
    default = next(r for r in data["routes"] if r["proto"] == "ipv4" and r["destination"] == "default")
    if route_via == "igc0":
        default.update(netif="igc0", gateway="100.64.0.1", intf_description="Starlink")
    return data


def wan(payload, wan_id: str) -> dict:
    return next(w for w in payload.stage["wans"] if w["id"] == wan_id)


def counters(fixture: str, iface: str) -> tuple[float, int, int]:
    data = load_json(fixture)
    entry = data["interfaces"][iface]
    return data["time"], int(entry["bytes received"]), int(entry["bytes transmitted"])


# ---- fetch -------------------------------------------------------------


async def test_fetch_uses_basic_auth_and_skips_tls_verification(http, collector):
    http.handler = recorded_handler()
    result = await collector.fetch()
    expected = "Basic " + base64.b64encode(b"the-key:the-secret").decode()
    assert {r.headers["authorization"] for r in http.requests} == {expected}
    assert all("the-key" not in str(r.url) and "the-secret" not in str(r.url) for r in http.requests)
    assert {r.url.path for r in http.requests} == set(ENDPOINT_FIXTURES)
    assert http.client_kwargs[0]["verify"] is False
    assert http.client_kwargs[0]["timeout"] == 10
    assert result["version"] == "26.7.4_1"
    assert collector.degraded is None


async def test_tls_verification_is_opt_in(http, monkeypatch, opn_env, defaults_config):
    monkeypatch.setenv("OPNSENSE_VERIFY_SSL", "1")
    http.handler = recorded_handler()
    await OpnsenseCollector(defaults_config).fetch()
    assert http.client_kwargs[0]["verify"] is True


async def test_core_failure_fails_the_poll(http, collector):
    http.handler = recorded_handler({"/api/routes/gateway/status"})
    with pytest.raises(httpx.HTTPStatusError):
        await collector.fetch()


async def test_health_failure_degrades_but_keeps_last_stats(http, collector):
    http.handler = recorded_handler()
    await collector.fetch()
    http.requests.clear()
    http.handler = recorded_handler({"/api/diagnostics/firewall/pf_states"})
    result = await collector.fetch()
    assert collector.degraded == "firewall stats unavailable: states"
    assert result["health"]["states"] == load_json("opnsense_pf_states.json")
    # The version is refreshed hourly, not every poll.
    assert "/api/diagnostics/system/systemInformation" not in {r.url.path for r in http.requests}
    http.handler = recorded_handler()
    await collector.fetch()
    assert collector.degraded is None


# ---- shape: the recorded firewall ---------------------------------------


def test_recorded_firewall_is_on_the_primary_with_the_backup_unplugged(collector):
    payload = collector.shape(raw())
    stage = payload.stage
    assert stage["active"]["id"] == "wan"
    assert stage["active"]["ip"] == "203.0.113.68"
    assert stage["active"]["since"] is None  # no change seen yet
    assert (stage["failover"], stage["offline"]) == (False, False)
    # One card per WAN interface: the IPv6 WAN_DHCP6 gateway doesn't get its own.
    assert [w["id"] for w in stage["wans"]] == ["wan", "opt9"]
    primary, starlink = wan(payload, "wan"), wan(payload, "opt9")
    assert (primary["health"], primary["active"], primary["kind"]) == ("up", True, "wired")
    assert primary["latency_ms"] > 0 and primary["loss_pct"] == 0
    assert (starlink["health"], starlink["link"], starlink["kind"]) == ("nolink", "no carrier", "satellite")
    assert starlink["line_bps"] is None  # a dead port's autoselect default is not a line rate
    assert starlink["latency_ms"] is None and starlink["gateway_ip"] is None
    # First poll: nothing to difference against yet.
    assert primary["down_bps"] is None and primary["history"]["down"] == []
    assert stage["system"] == {
        "version": "26.7.4_1", "uptime": "20h 41m", "load": 0.23, "mem_pct": 11.5,
        "states": 13477, "states_limit": 3254600,
    }
    texts = [(t.text, t.accent, t.priority, t.icon) for t in payload.tape]
    assert texts[0][0].startswith("WAN 8 ms · 0% loss")
    assert texts[0][1:] == ("neutral", 1, "wired")
    assert texts[1] == ("Starlink backup offline · no carrier", "down", 0, "satellite")


def test_backup_warning_can_be_turned_off(opn_env, defaults_config):
    defaults_config["modules"]["opnsense"]["warn_backup_down"] = False
    payload = OpnsenseCollector(defaults_config).shape(raw())
    assert [t.text for t in payload.tape if "backup" in t.text] == []
    assert wan(payload, "opt9")["health"] == "nolink"  # the card still says so


# ---- rates ---------------------------------------------------------------


def test_rates_come_from_counter_deltas_on_the_firewall_clock(collector):
    collector.shape(raw())
    payload = collector.shape(raw("opnsense_traffic_2.json"))
    t1, rx1, tx1 = counters("opnsense_traffic.json", "wan")
    t2, rx2, tx2 = counters("opnsense_traffic_2.json", "wan")
    primary = wan(payload, "wan")
    assert primary["down_bps"] == pytest.approx((rx2 - rx1) * 8 / (t2 - t1))
    assert primary["up_bps"] == pytest.approx((tx2 - tx1) * 8 / (t2 - t1))
    assert len(primary["history"]["down"]) == 1
    assert len(primary["history"]["latency"]) == 2
    assert "▼" in payload.tape[0].text


def test_vlan_parent_reports_only_its_untagged_traffic(collector):
    collector.shape(raw())
    payload = collector.shape(raw("opnsense_traffic_2.json"))
    vlans = {v["name"]: v for v in payload.stage["vlans"]}
    # WANs, loopback, IPsec and WireGuard are not networks on this list.
    assert set(vlans) == {"LAN", "Backups", "MGMT", "OPT3", "ProdServers", "Security",
                          "SmartDevices", "WebServers"}
    t1 = load_json("opnsense_traffic.json")
    t2 = load_json("opnsense_traffic_2.json")
    dt = t2["time"] - t1["time"]

    def tx_bps(iface: str) -> float:
        return (int(t2["interfaces"][iface]["bytes transmitted"])
                - int(t1["interfaces"][iface]["bytes transmitted"])) * 8 / dt

    children = ["opt1", "opt2", "opt3", "opt4", "opt5", "opt6", "opt8"]  # vlan02..vlan010 on ix1
    assert vlans["LAN"]["down_bps"] == pytest.approx(
        max(0.0, tx_bps("lan") - sum(tx_bps(c) for c in children))
    )
    # A VLAN's download is what the firewall transmits into it.
    assert vlans["Security"]["down_bps"] == pytest.approx(tx_bps("opt6"))
    totals = [(v["down_bps"] or 0) + (v["up_bps"] or 0) for v in payload.stage["vlans"]]
    assert totals == sorted(totals, reverse=True)


def test_vlan_exclude_hides_networks(opn_env, defaults_config):
    defaults_config["modules"]["opnsense"]["vlan_exclude"] = ["mgmt", "WebServers"]
    payload = OpnsenseCollector(defaults_config).shape(raw())
    assert {"MGMT", "WebServers"}.isdisjoint(v["name"] for v in payload.stage["vlans"])


def test_counter_reset_gives_no_rate(collector):
    collector.shape(raw("opnsense_traffic_2.json"))
    later = raw()  # the older, smaller counters...
    later["traffic"]["time"] += 60  # ...reported after the newer ones: a reset
    payload = collector.shape(later)
    assert wan(payload, "wan")["down_bps"] is None


def test_rates_survive_a_collector_restart(opn_env, defaults_config):
    OpnsenseCollector(defaults_config).shape(raw())
    payload = OpnsenseCollector(defaults_config).shape(raw("opnsense_traffic_2.json"))
    assert wan(payload, "wan")["down_bps"] is not None


# ---- which WAN -------------------------------------------------------------


def test_failover_follows_the_default_route(collector):
    collector.shape(raw())
    payload = collector.shape(failover_raw())
    stage = payload.stage
    assert stage["active"]["id"] == "opt9"
    assert stage["active"]["ip"] == "100.64.12.34"
    assert stage["active"]["since"] is not None  # the switch was seen
    assert (stage["failover"], stage["offline"]) == (True, False)
    assert wan(payload, "wan")["health"] == "down"
    top = max(payload.tape, key=lambda t: t.priority)
    assert (top.text, top.accent, top.icon) == (
        "FAILOVER · on Starlink · WAN gateway down", "alert", "satellite",
    )
    assert any(t.text.startswith("Starlink 32 ms") for t in payload.tape)
    assert not any("backup offline" in t.text for t in payload.tape)


def test_dead_default_route_falls_back_to_the_live_gateway(collector):
    """Gateway-group policy routing leaves the default route on the dead WAN."""
    payload = collector.shape(failover_raw(route_via="ix0"))
    assert payload.stage["active"]["id"] == "opt9"
    assert payload.stage["failover"] is True


def test_no_live_gateway_is_offline(collector):
    data = raw()
    for gw in data["gateway_status"]["items"]:
        gw.update(status="down", status_translated="Offline")
    payload = collector.shape(data)
    assert payload.stage["active"] is None
    assert (payload.stage["offline"], payload.stage["failover"]) == (True, False)
    assert payload.tape[0].text == "INTERNET DOWN · no WAN gateway online"
    assert payload.tape[0].accent == "alert"


def test_primary_interface_is_configurable(opn_env, defaults_config):
    defaults_config["modules"]["opnsense"]["primary_interface"] = "opt9"
    payload = OpnsenseCollector(defaults_config).shape(failover_raw())
    assert payload.stage["failover"] is False
    assert [w["id"] for w in payload.stage["wans"]] == ["opt9", "wan"]


# ---- formatting ------------------------------------------------------------


def test_formatters():
    assert _uptime("20:30:54") == "20h 30m"
    assert _uptime("3 days, 04:12:08") == "3d 4h"
    assert _uptime("1 day, 00:05") == "1d 0h"
    assert _uptime(None) is None
    assert fmt_rate(None) == "—"
    assert fmt_rate(472_979) == "473 kb/s"
    assert fmt_rate(42_100_000) == "42.1 Mb/s"
    assert fmt_rate(940_000_000) == "940 Mb/s"
    assert fmt_rate(2_350_000_000) == "2.35 Gb/s"
