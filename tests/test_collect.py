import json

import pytest
from omini_sdk import PluginError

import omini_omada.client as client_module
from omini_omada.collect import collect, mac, uptime_seconds
from omini_omada.collect import test as connection_test


def by_key(devices):
    return {d.key: d for d in devices}


def test_one_device_per_gateway_switch_and_ap(omada, cfg):
    devices = collect(cfg)
    # Disconnected (…:05) and pending (…:06) devices are left out.
    assert [d.key for d in devices] == [
        "50:c7:bf:00:00:01",
        "50:c7:bf:00:00:02",
        "50:c7:bf:00:00:03",
        "50:c7:bf:00:00:04",
    ]
    gw, sw, ap, mesh = devices
    assert (gw.role, sw.role, ap.role, mesh.role) == ("router", "switch", "ap", "ap")
    assert (ap.name, ap.model, ap.vendor, ap.host) == (
        "AP Living room",
        "EAP660 HD",
        "TP-Link",
        "192.168.0.20",
    )
    assert ap.serial == "22312345678901" and [m.root for m in ap.macs] == ["50:c7:bf:00:00:03"]
    assert ap.uptime_s == ((3 * 24 + 4) * 60 + 12) * 60 + 9
    assert (ap.cpu_pct, ap.mem_pct) == (7, 61)
    assert ap.os_version == "1.2.3 Build 20240110 Rel. 51111"


def test_pending_firmware_from_the_controller(omada, cfg):
    _, sw, ap, _ = collect(cfg)
    assert ap.firmware.update_available is True
    assert ap.firmware.latest == "1.3.0 Build 20240520 Rel. 70001"
    assert ap.firmware.checked_at is None  # the controller does not say when it checked
    assert sw.firmware.update_available is False
    assert sw.firmware.current == "1.30.4 Build 20231020 Rel.41234"


def test_read_only(omada, cfg):
    collect(cfg)
    connection_test(cfg)
    # Only reads: the token request is the one POST.
    assert {m for m, p in omada.requests if p != "/openapi/authorize/token"} == {"GET"}


def test_switch_ports_counters_and_sfp(omada, cfg):
    sw = by_key(collect(cfg))["50:c7:bf:00:00:02"]
    ports = {i.name: i for i in sw.interfaces}
    assert list(ports) == ["Port 1", "Port 5", "Port 6", "Port 25"]
    p1, p5, p6, p25 = ports.values()
    assert (p1.up, p1.speed_mbps, p1.connector, p1.rx_bytes, p1.tx_bytes) == (
        True,
        1000,
        "rj45",
        2222,
        1111,
    )
    assert p1.description is None  # the default name
    assert p5.description == "AP Living room" and p5.speed_mbps == 2500
    assert (p6.up, p6.speed_mbps) == (False, None)
    assert (p25.connector, p25.speed_mbps) == ("sfp", 10000)
    t = p25.transceiver
    assert (t.temperature_c, t.rx_power_dbm, t.tx_power_dbm, t.bias_ma, t.voltage_v) == (
        38.5,
        -3.98,
        -3.01,
        6.5,
        3.3,
    )


def test_neighbors_lldp_and_uplinks(omada, cfg):
    devices = by_key(collect(cfg))
    sw = devices["50:c7:bf:00:00:02"]
    lldp, up = sw.neighbors
    assert (
        lldp.protocol,
        lldp.local_port,
        lldp.remote_mac,
        lldp.remote_port,
        lldp.remote_name,
    ) == (
        "lldp",
        "Port 1",
        "00:11:32:aa:bb:cc",
        "eth0",
        "nas",
    )
    # The switch's uplink (its port 25) goes to gateway port 2, named after the gateway's port.
    assert (up.protocol, up.local_port, up.remote_mac, up.remote_port) == (
        "other",
        "Port 25",
        "50:c7:bf:00:00:01",
        "LAN2",
    )
    [ap_up] = devices["50:c7:bf:00:00:03"].neighbors
    assert (ap_up.local_port, ap_up.remote_mac, ap_up.remote_port) == (
        "ETH1",
        "50:c7:bf:00:00:02",
        "Port 5",
    )
    [mesh] = devices["50:c7:bf:00:00:04"].neighbors
    assert (mesh.local_port, mesh.remote_mac) == ("Wi-Fi backhaul", "50:c7:bf:00:00:03")


def test_gateway_wans_ports_and_health(omada, cfg):
    gw = by_key(collect(cfg))["50:c7:bf:00:00:01"]
    ports = {i.name: i for i in gw.interfaces}
    wan1, wan3, lan2 = ports["WAN1"], ports["WAN/LAN3"], ports["LAN2"]
    assert (wan1.wan, wan1.up, wan1.speed_mbps, wan1.duplex) == (True, True, 1000, "full")
    assert wan1.ips == ["203.0.113.10/24"] and wan1.mac == "50:c7:bf:00:00:11"
    assert wan1.rx_bytes == 98765432100 and wan1.description == "Fiber ISP"
    assert (wan3.wan, wan3.up, wan3.speed_mbps) == (True, False, None)
    assert (lan2.wan, lan2.up, lan2.speed_mbps) == (None, True, 1000)
    gws = {g.name: g for g in gw.gateways}
    assert (gws["WAN1"].status, gws["WAN1"].rtt_ms, gws["WAN1"].loss_pct) == ("up", 4, 0)
    assert gws["WAN1"].address == "203.0.113.1" and gws["WAN1"].interface == "WAN1"
    assert gws["WAN/LAN3"].status == "down"
    assert [(t.sensor, t.celsius) for t in gw.temperatures] == [("System", 47)]
    assert [(v.id, v.name) for v in gw.vlans] == [(1, "LAN"), (20, "IOT")]
    assert gw.ips == ["192.168.0.1", "192.168.20.1"]
    assert gw.uptime_s == 10 * 86400 + 5


def test_access_point_ports_and_radios(omada, cfg):
    ap = by_key(collect(cfg))["50:c7:bf:00:00:03"]
    ports = {i.name: i for i in ap.interfaces}
    assert list(ports) == ["ETH1", "2.4 GHz", "5 GHz"]
    assert (ports["ETH1"].speed_mbps, ports["ETH1"].rx_bytes) == (2500, 333333)
    assert (ports["5 GHz"].type, ports["5 GHz"].rx_bytes, ports["5 GHz"].tx_bytes) == (
        "wireless",
        3000,
        4000,
    )


def test_clients(omada, cfg):
    devices = by_key(collect(cfg))
    ap, mesh = devices["50:c7:bf:00:00:03"], devices["50:c7:bf:00:00:04"]
    [phone] = ap.wireless_clients
    assert (phone.mac, phone.interface, phone.ssid, phone.band) == (
        "aa:bb:cc:dd:ee:01",
        "Home · 5 GHz",
        "Home",
        "5ghz",
    )
    assert (phone.signal_dbm, phone.tx_rate_mbps, phone.rx_rate_mbps) == (-52, 1200, 864)
    assert (phone.rx_bps, phone.tx_bps) == (1_000_000, 20_000)
    [plug] = mesh.wireless_clients
    assert (plug.interface, plug.band, plug.rx_bps) == ("IOT · 2.4 GHz", "2.4ghz", 0)

    sw, gw = devices["50:c7:bf:00:00:02"], devices["50:c7:bf:00:00:01"]
    assert [(f.mac, f.port, f.vlan) for f in sw.fdb] == [("00:11:32:aa:bb:cc", "Port 1", 1)]
    assert [(f.mac, f.port) for f in gw.fdb] == [("aa:bb:cc:dd:ee:04", "LAN2")]

    hosts = {h.mac: h for d in devices.values() for h in d.hosts or []}
    assert "aa:bb:cc:dd:ee:05" not in hosts  # offline client
    assert hosts["aa:bb:cc:dd:ee:01"].hostnames == ["Ricardo's phone", "pixel-8"]
    assert hosts["aa:bb:cc:dd:ee:01"].os == "Android 15"
    assert hosts["aa:bb:cc:dd:ee:02"].hostnames == ["esp-plug"]  # a MAC is not a name
    assert hosts["00:11:32:aa:bb:cc"].sources == ["omada"]


def test_other_site_by_name(omada, cfg):
    cfg["site"] = "office"
    office = "650aa0d1b3f2ae5b91227600"
    omada.routes[
        f"/openapi/v1/{omada.routes['/api/info']['result']['omadacId']}/sites/{office}/devices"
    ] = {
        "errorCode": 0,
        "msg": "Success.",
        "result": {"totalRows": 0, "currentPage": 1, "currentSize": 1000, "data": []},
    }
    assert collect(cfg) == []
    cfg["site"] = "Lab"
    with pytest.raises(PluginError, match=r'no site named "Lab" \(sites: Default, Office\)'):
        collect(cfg)


def test_pages_are_followed(omada, cfg, monkeypatch):
    monkeypatch.setattr(client_module, "PAGE_SIZE", 2)
    devices = collect(cfg)
    assert len(devices) == 4
    assert (
        omada.calls.count(
            "/openapi/v1/de382a0e78f4deb681f3128c3e75dbd1/sites/640effd1b3f2ae5b912275ec/devices"
        )
        == 3
    )


def test_connection_test(omada, cfg):
    msg = connection_test(cfg)
    assert msg == (
        'Connected to Omada Controller 5.15.20.20, site "Default": '
        "1 gateway, 1 switch, 4 access points"
    )


def test_wrong_credentials(omada, cfg):
    cfg["client_secret"] = "wrong"
    with pytest.raises(PluginError, match="rejected the client ID or secret"):
        connection_test(cfg)
    with pytest.raises(PluginError, match="rejected the client ID or secret"):
        collect(cfg)


def test_secret_never_in_errors_or_state(omada, cfg):
    cfg["client_secret"] = "wrong"
    with pytest.raises(PluginError) as e:
        collect(cfg)
    assert "wrong" not in str(e.value)
    cfg["client_secret"] = "client-secret"
    collect(cfg)
    saved = (cfg.state_dir / "token.json").read_text()
    assert "client-secret" not in saved
    assert (cfg.state_dir / "token.json").stat().st_mode & 0o077 == 0


def test_token_is_cached_and_renewed(omada, cfg):
    collect(cfg)
    assert omada.token_requests == 1
    collect(cfg)  # the cached token is reused
    assert omada.token_requests == 1
    omada.expire_tokens()  # -44112: a new token is requested once
    assert len(collect(cfg)) == 4
    assert omada.token_requests == 2
    data = json.loads((cfg.state_dir / "token.json").read_text())
    assert data["accessToken"] in omada.tokens


def test_other_credentials_do_not_reuse_the_cached_token(omada, cfg):
    collect(cfg)
    omada.client = ("other-id", "other-secret")
    cfg["client_id"], cfg["client_secret"] = "other-id", "other-secret"
    collect(cfg)
    assert omada.token_requests == 2


def test_optional_endpoints_failing_do_not_fail_the_collection(omada, cfg):
    site = "sites/640effd1b3f2ae5b912275ec"
    omada.forbidden = {
        f"{site}/clients",
        f"{site}/stat/",
        f"{site}/switches/",
        f"{site}/gateways/",
        f"{site}/grid/",
        f"{site}/aps/50-C7-BF-00-00-03/radios",
    }
    devices = by_key(collect(cfg))
    assert len(devices) == 4
    sw, gw = devices["50:c7:bf:00:00:02"], devices["50:c7:bf:00:00:01"]
    assert sw.interfaces is None and sw.fdb is None
    # The device list still gives the uplink, with the gateway's port as reported.
    [up] = sw.neighbors
    assert (up.local_port, up.remote_mac, up.remote_port) == ("Uplink", "50:c7:bf:00:00:01", "2")
    assert gw.gateways is None and gw.cpu_pct == 12
    assert sw.firmware.update_available is None  # unknown, not "no update"
    assert [i.name for i in devices["50:c7:bf:00:00:03"].interfaces] == ["ETH1"]


def test_devices_forbidden_is_a_clear_error(omada, cfg):
    omada.forbidden = {"sites/640effd1b3f2ae5b912275ec/devices"}
    with pytest.raises(PluginError, match="role and site privileges"):
        collect(cfg)


def test_not_an_omada_controller(omada, cfg):
    omada.routes["/api/info"] = {"errorCode": -1, "msg": "nope"}
    with pytest.raises(PluginError, match="is it an Omada controller"):
        connection_test(cfg)


def test_missing_fields(cfg):
    cfg["client_id"] = ""
    with pytest.raises(PluginError, match="required"):
        collect(cfg)


def test_unreachable_host(cfg):
    cfg["url"] = "https://127.0.0.1:9"
    with pytest.raises(PluginError, match="cannot connect"):
        connection_test(cfg)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("50-C7-BF-00-00-03", "50:c7:bf:00:00:03"),
        ("50:C7:BF:00:00:03", "50:c7:bf:00:00:03"),
        ("50c7.bf00.0003", "50:c7:bf:00:00:03"),
        ("00-00-00-00-00-00", None),
        ("nas", None),
        ("50-C7-BF-00-00", None),
        (None, None),
    ],
)
def test_mac_normalization(raw, normalized):
    assert mac(raw) == normalized


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("3day(s) 4h 12m 9s", 274329),
        ("0day(s) 0h 5m 0s", 300),
        ("2 days 01:00:00", 176400),
        ("04:12:09", 15129),
        (93600, 93600),
        ("", None),
        ("unknown", None),
    ],
)
def test_uptime(raw, seconds):
    assert uptime_seconds(raw) == seconds
