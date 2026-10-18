import json
import re

import httpx
import pytest
from omini_sdk import Change, PluginError

import omini_omada.collect as collect_module
from tests.conftest import OMADAC_ID

SITE = "640effd1b3f2ae5b912275ec"
BASE = f"/openapi/v1/{OMADAC_ID}/sites/{SITE}/wireless-network"


@pytest.fixture
def site(omada):
    """The site's WLAN group with two SSIDs, and the writes Wi-Fi needs."""
    group = {"wlanId": "g1", "name": "Default"}
    omada.ssids = [
        {
            "ssidId": "s1",
            "name": "Office",
            "band": 3,
            "security": 3,
            "broadcast": True,
            "vlanSetting": {"mode": 0},
            "pskSetting": {"versionPsk": 2},
        },
        {
            "ssidId": "s2",
            "name": "Guests",
            "band": 3,
            "security": 3,
            "broadcast": True,
            "guestNetEnable": True,
            "vlanSetting": {"mode": 1, "customConfig": {"vlanId": 30}},
            "pskSetting": {"versionPsk": 3},
            "ssidEnable": True,
        },
    ]
    omada.sent = []
    inner = omada.handler

    def page(rows):
        return {
            "errorCode": 0,
            "msg": "",
            "result": {"totalRows": len(rows), "currentPage": 1, "currentSize": 100, "data": rows},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(BASE):
            return inner(request)
        inner(httpx.Request("GET", request.url.copy_with(path="/api/info")))  # count it
        body = json.loads(request.content) if request.content else {}
        if request.method != "GET":
            omada.sent.append((request.method, path[len(BASE) :], body))
        if path == f"{BASE}/wlans":
            return httpx.Response(200, json=page([group]))
        if path == f"{BASE}/wlans/g1/ssids" and request.method == "GET":
            return httpx.Response(200, json=page(omada.ssids))
        if path == f"{BASE}/wlans/g1/ssids" and request.method == "POST":
            omada.ssids.append(
                {
                    "ssidId": f"s{len(omada.ssids) + 1}",
                    **{k: v for k, v in body.items() if k != "pskSetting"},
                }
            )
            return httpx.Response(200, json={"errorCode": 0, "msg": "Success."})
        m = re.fullmatch(rf"{BASE}/(?:wlans/g1/)?ssids/(\w+)(/.*)?", path)
        s = next(x for x in omada.ssids if x["ssidId"] == m.group(1))
        if request.method == "DELETE":
            omada.ssids.remove(s)
        elif m.group(2) == "/update-basic-config":
            s.update({k: v for k, v in body.items() if k != "pskSetting"})
        elif m.group(2) == "/enable":
            s["ssidEnable"] = body["ssidEnable"]
        return httpx.Response(200, json={"errorCode": 0, "msg": "Success."})

    omada.handler = handler
    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    return omada


def aps_wifi(devices):
    aps = [d for d in devices if d.role == "ap"]
    assert aps
    return {w.ssid: w for w in aps[0].wifi_networks}


def test_every_ap_reports_the_sites_wifi(site, cfg):
    got = aps_wifi(collect_module.collect(cfg))
    assert (got["Office"].vlan, got["Office"].security) == (None, "wpa2")
    assert (got["Guests"].vlan, got["Guests"].security, got["Guests"].enabled) == (
        30,
        "wpa2wpa3",
        True,
    )


def test_creates_a_wifi_on_a_vlan(site, cfg):
    value = {"ssid": "Cameras", "vlan": 40, "password": "correct horse"}
    devices = collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    method, path, body = site.sent[0]
    assert (method, path) == ("POST", "/wlans/g1/ssids")
    assert body["vlanSetting"] == {"mode": 1, "customConfig": {"vlanId": 40}}
    assert body["pskSetting"]["securityKey"] == "correct horse"
    assert body["security"] == 3
    assert aps_wifi(devices)["Cameras"].vlan == 40


def test_moves_renames_and_turns_off_a_wifi(site, cfg):
    value = {"ssid": "Visitors", "previous": "Guests", "vlan": 80, "enabled": False}
    devices = collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    patch = next(b for m, p, b in site.sent if p.endswith("update-basic-config"))
    assert patch["name"] == "Visitors" and "pskSetting" not in patch  # no password: kept
    assert ("PATCH", "/ssids/s2/enable", {"ssidEnable": False}) in site.sent
    got = aps_wifi(devices)["Visitors"]
    assert (got.vlan, got.enabled) == (80, False)


def test_deletes_a_wifi(site, cfg):
    devices = collect_module.apply(
        cfg, Change(device="x", kind="wifi.delete", value={"ssid": "Guests"})
    )
    assert "Guests" not in aps_wifi(devices)
    with pytest.raises(PluginError, match="no Wi-Fi network"):
        collect_module.apply(cfg, Change(device="x", kind="wifi.delete", value={"ssid": "Nope"}))


def test_a_forbidden_wifi_change_names_the_site_settings_permission(site, cfg):
    # SSIDs are site settings: the role needs Modify on Site Settings, not
    # only on devices.
    inner = site.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" and request.url.path.startswith(BASE):
            return httpx.Response(200, json={"errorCode": -1005, "msg": "Operation forbidden."})
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    value = {"ssid": "Cameras", "vlan": 40, "password": "correct horse"}
    with pytest.raises(PluginError, match="Site Settings"):
        collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))


def test_an_enterprise_wifi_has_no_personal_security(site, cfg):
    # security 2 is WPA-Enterprise, which the data model has no value for:
    # left empty rather than reported as WPA2 personal.
    site.ssids[0]["security"] = 2
    assert aps_wifi(collect_module.collect(cfg))["Office"].security is None


def test_a_new_wifi_is_wpa2_personal(site, cfg):
    value = {"ssid": "Cameras", "vlan": 40, "password": "correct horse"}
    collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    body = site.sent[0][2]
    assert (body["security"], body["pskSetting"]["versionPsk"]) == (3, 2)


@pytest.fixture
def two_groups(site):
    """A second WLAN group with its own "Office" SSID."""
    other = {"ssidId": "s9", "name": "Office", "band": 3, "security": 3}
    inner = site.handler

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{BASE}/wlans":
            rows = [{"wlanId": "g1", "name": "Default"}, {"wlanId": "g2", "name": "Branch"}]
            body = {"totalRows": 2, "currentPage": 1, "currentSize": 100, "data": rows}
            return httpx.Response(200, json={"errorCode": 0, "msg": "", "result": body})
        if path == f"{BASE}/wlans/g2/ssids" and request.method == "GET":
            body = {"totalRows": 1, "currentPage": 1, "currentSize": 100, "data": [other]}
            return httpx.Response(200, json={"errorCode": 0, "msg": "", "result": body})
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    return site


def test_a_name_in_several_wlan_groups_is_refused(two_groups, cfg):
    with pytest.raises(PluginError, match="several WLAN groups"):
        collect_module.apply(cfg, Change(device="x", kind="wifi.delete", value={"ssid": "Office"}))
    value = {"ssid": "Office", "vlan": 40}
    with pytest.raises(PluginError, match="several WLAN groups"):
        collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    value = {"ssid": "Main", "previous": "Office", "vlan": 40}
    with pytest.raises(PluginError, match="several WLAN groups"):
        collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    assert two_groups.sent == []
    # A name in one group only is changed as before.
    value = {"ssid": "Guests", "vlan": 40}
    collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    assert any(p.endswith("/s2/update-basic-config") for _m, p, _b in two_groups.sent)
