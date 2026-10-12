"""Spanning tree and LAGs of the switch ports, and configuration backups."""

import json

import pytest
from omini_sdk import PluginError

from omini_omada.collect import backup, collect
from tests.conftest import OMADAC_ID, SITE
from tests.test_vlans import lan  # noqa: F401  (fixture)
from tests.test_wifi import site  # noqa: F401  (fixture)

GATEWAY = "50:c7:bf:00:00:01"
SWITCH = "50:c7:bf:00:00:02"
STAT = f"/openapi/v1/{OMADAC_ID}/sites/{SITE}/stat/switches/50-C7-BF-00-00-02"
DEVICES = f"/openapi/v1/{OMADAC_ID}/sites/{SITE}/devices"


def stat_ports(omada):
    return {p["port"]: p for p in omada.routes[STAT]["result"]["ports"]}


def ports(devices):
    sw = next(d for d in devices if d.key == SWITCH)
    return {i.name: i for i in sw.interfaces}


# --- STP -----------------------------------------------------------------------


def test_stp_state_from_discarding(omada, cfg):
    p = stat_ports(omada)
    p[5]["portStatus"]["stpDiscarding"] = True
    p[6]["portStatus"]["stpDiscarding"] = False
    got = ports(collect(cfg))
    assert got["Port 1"].stp.state == "forwarding" and got["Port 1"].stp.role is None
    assert got["Port 5"].stp.state == "blocking"
    assert got["Port 6"].stp.state == "disabled"  # its link is down
    assert got["Port 25"].stp is None  # not reported
    assert all(d.stp_root is None for d in collect(cfg))


# --- LAG -----------------------------------------------------------------------


def test_lag_members_and_the_lag(omada, cfg):
    p = stat_ports(omada)
    p[1]["lagId"] = 1
    p[25]["lagId"] = 1
    p[6]["lagId"] = 2
    got = ports(collect(cfg))
    assert (got["Port 1"].lag, got["Port 25"].lag, got["Port 6"].lag) == ("LAG 1", "LAG 1", "LAG 2")
    assert got["Port 5"].lag is None
    lag1, lag2 = got["LAG 1"], got["LAG 2"]
    assert lag1.type == "lag" and lag1.up is True and lag1.speed_mbps == 1000 + 10000
    assert lag2.up is False and lag2.speed_mbps is None
    assert [i.name for i in ports(collect(cfg)).values() if i.type == "lag"] == ["LAG 1", "LAG 2"]


def test_no_lag_without_lag_ids(omada, cfg):
    assert not any(i.type == "lag" for i in ports(collect(cfg)).values())


# --- backup ----------------------------------------------------------------------


@pytest.fixture
def full(site, lan):  # noqa: F811
    """The site's SSIDs (with their passwords, as the controller has them),
    LAN networks and profiles, and the switch's port list."""
    site.ssids[0]["pskSetting"]["securityKey"] = "office-pass"
    site.ssids[1]["pskSetting"]["securityKey"] = "guest-pass"
    site.ssids[1]["radiusSetting"] = {"radiusPwd": "radius-pwd", "port": 1812}
    lan.ports[0]["portStatus"] = {"linkStatus": 1, "tx": 1}
    return site


def contents(cfg):
    return {b.device: json.loads(b.content) for b in backup(cfg)}


def test_one_backup_per_device_collect_reports(full, cfg):
    out = backup(cfg)
    assert [b.device for b in out] == sorted(d.key for d in collect(cfg))
    assert {b.format for b in out} == {"json"}


def test_site_settings_with_the_gateway_without_secrets(full, cfg):
    out = contents(cfg)
    assert [k for k, v in out.items() if "site" in v] == [GATEWAY]
    s = out[GATEWAY]["site"]
    assert [n["name"] for n in s["networks"]] == ["Cameras", "Default", "Users", "Voice"]
    assert "AP" in [p["name"] for p in s["port_profiles"]]
    assert [w["name"] for w in s["wifi_networks"]] == ["Guests", "Office"]
    assert s["wifi_networks"][0]["pskSetting"] == {"versionPsk": 3}
    assert s["wifi_networks"][0]["radiusSetting"] == {"port": 1812}
    text = "".join(b.content for b in backup(cfg))
    for secret in ("office-pass", "guest-pass", "radius-pwd", "securityKey"):
        assert secret not in text


def test_device_settings_without_status(full, cfg):
    out = contents(cfg)
    sw = out[SWITCH]["device"]
    assert sw["name"] == "Core switch" and sw["mac"] == SWITCH
    assert [p["port"] for p in sw["portList"]] == [1, 5, 6, 25]
    assert sw["portList"][0] == {
        "port": 1,
        "profileId": "pAll",
        "profileOverrideEnable": False,
        "lagPort": False,
    }
    gw = out[GATEWAY]["device"]
    assert [p["port"] for p in gw["portConfigs"]] == [1, 2]
    for live in ("uptime", "cpuUtil", "memUtil", "temp", "lastSeen", "status", "firmwareVersion"):
        assert all(live not in c["device"] for c in out.values())


def test_without_a_gateway_the_switch_holds_the_site(full, cfg):
    rows = full.routes[DEVICES]["result"]["data"]
    full.routes[DEVICES]["result"]["data"] = [d for d in rows if d["type"] != "gateway"]
    assert [k for k, v in contents(cfg).items() if "site" in v] == [SWITCH]


def test_backup_is_deterministic(full, lan, cfg):  # noqa: F811
    first = [b.content for b in backup(cfg)]
    # Status changes and the controller lists things in another order.
    for p in stat_ports(full).values():
        p["portStatus"]["tx"] = (p["portStatus"].get("tx") or 0) + 999
    lan.ports[0]["portStatus"] = {"linkStatus": 0, "tx": 2}
    lan.nets.reverse()
    lan.profiles.reverse()
    lan.ports.reverse()
    full.ssids.reverse()
    full.routes[DEVICES]["result"]["data"].reverse()
    assert [b.content for b in backup(cfg)] == first


def test_backup_changes_with_the_configuration(full, lan, cfg):  # noqa: F811
    before = contents(cfg)
    lan.ports[1]["profileId"] = "pUsers"
    after = contents(cfg)
    assert after[SWITCH] != before[SWITCH]
    assert after[GATEWAY] == before[GATEWAY]


def test_unreadable_settings_are_left_out(omada, cfg):
    # A role that may not read the switch's settings; a controller without
    # the LAN and Wi-Fi endpoints (404).
    omada.forbidden.add(f"sites/{SITE}/switches/")
    out = contents(cfg)
    assert "portList" not in out[SWITCH]["device"] and out[SWITCH]["device"]["name"]
    assert out[GATEWAY]["site"] == {}


def test_a_failing_read_fails_the_backup(omada, cfg, monkeypatch):
    """A controller error is not a missing endpoint: no half-read backup."""
    from omini_omada.client import Client, Unavailable

    pages = Client.pages

    def failing(self, path, *a, **kw):
        if "lan-networks" in path:
            raise Unavailable(f"{path}: HTTP 500")
        return pages(self, path, *a, **kw)

    monkeypatch.setattr(Client, "pages", failing)
    with pytest.raises(PluginError, match="cannot read the site's networks"):
        backup(cfg)


def test_backup_without_devices(omada, cfg):
    omada.routes[DEVICES]["result"]["data"] = []
    with pytest.raises(PluginError, match="no connected device"):
        backup(cfg)
