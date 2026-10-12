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
