"""VLANs: the site's LAN networks and the switch ports' profiles, against a
stateful fake of the Open API endpoints that read and change them."""

import json
import re

import httpx
import pytest
from omini_sdk import Change, PluginError

import omini_omada.collect as collect_module
import omini_omada.vlans as vlans_module
from tests.conftest import OMADAC_ID, SITE

SWITCH = "50:c7:bf:00:00:02"
SW_PATH = "50-C7-BF-00-00-02"
V1 = f"/openapi/v1/{OMADAC_ID}/sites/{SITE}"
V2 = f"/openapi/v2/{OMADAC_ID}/sites/{SITE}"
BASE = {  # a profile's settings besides its VLANs, as the controller lists them
    "poe": 2,
    "dot1x": 1,
    "portIsolationEnable": False,
    "lldpMedEnable": True,
    "topoNotifyEnable": False,
    "bandWidthCtrlType": 0,
    "spanningTreeEnable": False,
    "loopbackDetectEnable": False,
    "eeeEnable": False,
    "flowControlEnable": False,
    "loopbackDetectVlanBasedEnable": False,
    "dhcpL2RelaySettings": {"enable": False},
}


def ok(result=None):
    body = {"errorCode": 0, "msg": "Success."}
    if result is not None:
        body["result"] = result
    return httpx.Response(200, json=body)


def fail(code, msg):
    return httpx.Response(200, json={"errorCode": code, "msg": msg})


def page(rows):
    return ok({"totalRows": len(rows), "currentPage": 1, "currentSize": 1000, "data": rows})


class Lan:
    """The site's LAN networks and profiles and the switch's port profiles."""

    def __init__(self):
        self.nets = [
            {
                "id": "n1",
                "name": "Default",
                "purpose": 1,
                "vlanType": 0,
                "vlan": 1,
                "gatewaySubnet": "192.168.0.1/24",
                "igmpSnoopEnable": False,
                "primary": True,
                "portal": False,
            },
            {"id": "n10", "name": "Users", "purpose": 0, "vlan": 10, "igmpSnoopEnable": True},
            {"id": "n20", "name": "Voice", "purpose": 0, "vlan": 20, "igmpSnoopEnable": False},
            {
                "id": "n30",
                "name": "Cameras",
                "purpose": 1,
                "vlanType": 0,
                "vlan": 30,
                "gatewaySubnet": "192.168.30.1/24",
                "igmpSnoopEnable": False,
                "existRA": False,
            },
        ]
        self.profiles = [
            {"id": "pAll", "name": "All", "flag": 0, "type": 0, "nativeNetworkId": "n1", **BASE},
            {"id": "pOff", "name": "Disable", "flag": 0, "type": 1, **BASE},
            {
                "id": "pUsers",
                "name": "Users",
                "flag": 1,
                "type": 2,
                "nativeNetworkId": "n10",
                **BASE,
            },
            {
                "id": "pVoice",
                "name": "Voice",
                "flag": 1,
                "type": 2,
                "nativeNetworkId": "n20",
                **BASE,
            },
            {
                "id": "pAP",
                "name": "AP",
                "flag": 2,
                "type": 2,
                "nativeNetworkId": "n1",
                "tagNetworkIds": ["n10", "n20"],
                "untagNetworkIds": [],
                **BASE,
            },
            {
                "id": "pNoPoe",
                "name": "Users no PoE",
                "flag": 2,
                "type": 2,
                "nativeNetworkId": "n10",
                "tagNetworkIds": [],
                **{**BASE, "poe": 1},
            },
        ]
        self.ports = [
            {"port": 1, "profileId": "pAll", "profileOverrideEnable": False, "lagPort": False},
            {"port": 5, "profileId": "pAP", "profileOverrideEnable": False, "lagPort": False},
            {"port": 6, "profileId": "pUsers", "profileOverrideEnable": False, "lagPort": False},
            {"port": 25, "profileId": "pAll", "profileOverrideEnable": False, "lagPort": True},
        ]
        self.ids = 0
        self.v2 = True  # False: a controller older than 5.15 (v1 only)
        self.ignore = False  # answers the profile PUT but changes nothing
        self.sent = []

    def new_id(self, prefix):
        self.ids += 1
        return f"{prefix}{self.ids:04d}"

    def handle(self, request: httpx.Request, path: str) -> httpx.Response | None:
        method = request.method
        body = json.loads(request.content) if request.content else {}
        version = "v2" if path.startswith(V2) else "v1"
        rel = path.removeprefix(V2 if version == "v2" else V1)
        if method != "GET":
            self.sent.append((method, version, rel, body))
        if rel.startswith("/lan-networks"):
            if version == "v2" and not self.v2:
                return httpx.Response(404, json={"errorCode": -1600, "msg": "Unsupported"})
            return self.networks(method, rel, body)
        if rel.startswith("/lan-profiles"):
            if method == "GET":
                return page(self.profiles)
            if method == "POST":
                if any(p["name"] == body["name"] for p in self.profiles):
                    return fail(-33506, "This Profile name already exists.")
                pid = self.new_id("p")
                self.profiles.append({"id": pid, "flag": 2, "type": 2, **body})
                return ok({"id": pid})
            if method == "DELETE":
                pid = rel.rsplit("/", 1)[1]
                self.profiles = [p for p in self.profiles if p["id"] != pid]
                return ok()
        if rel == f"/switches/{SW_PATH}" and method == "GET":
            return ok({"mac": SW_PATH, "portList": [dict(p) for p in self.ports]})
        m = re.fullmatch(rf"/switches/{SW_PATH}/ports/(\d+)/profile", rel)
        if m and method == "PUT":
            port = next((p for p in self.ports if p["port"] == int(m.group(1))), None)
            if port is None:
                return fail(-39701, "This port does not exist")
            if not any(p["id"] == body.get("profileId") for p in self.profiles):
                return fail(-33507, "This profile does not exist.")
            if not self.ignore:
                port["profileId"] = body["profileId"]
            return ok()
        return None

    def networks(self, method, rel, body):
        if method == "GET" and rel == "/lan-networks":
            return page([dict(n) for n in self.nets])
        if method == "POST" and rel == "/lan-networks":
            if any(n["name"] == body["name"] for n in self.nets):
                return fail(-33500, "Network name already existed.")
            nid = self.new_id("n")
            self.nets.append({"id": nid, **body})
            native = {"id": self.new_id("p"), "name": body["name"], "flag": 1, "type": 2}
            self.profiles.append({**native, "nativeNetworkId": nid, **BASE})
            return ok({"id": nid})
        nid = rel.rsplit("/", 1)[1]
        net = next((n for n in self.nets if n["id"] == nid), None)
        if net is None:
            return fail(-33503, "This network does not exist.")
        if method == "PATCH":
            net.clear()
            net.update({"id": nid, **body})
            return ok()
        if method == "DELETE":
            if net.get("primary"):
                return fail(-33505, "Default LAN cannot be deleted.")
            used = {p["profileId"] for p in self.ports}
            if any(p["id"] in used and p.get("nativeNetworkId") == nid for p in self.profiles):
                return fail(-33566, "The network has been configured as the switch's native")
            self.nets.remove(net)
            return ok()
        return None


@pytest.fixture
def lan(omada, monkeypatch):
    state = Lan()
    inner = omada.handler

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith((V1, V2)) and request.headers.get("authorization"):
            answer = state.handle(request, path)
            if answer is not None:
                return answer
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    state.omada = omada
    return state


def switch(devices):
    return next(d for d in devices if d.key == SWITCH)


def port_vlans(devices):
    out = {}
    for i in switch(devices).interfaces:
        if i.vlans is not None:
            out[i.name] = (i.vlans.untagged, [int(t.root) for t in i.vlans.tagged or []])
    return out


def run(cfg, kind, value, port=None, device=SWITCH):
    return collect_module.apply(cfg, Change(device=device, kind=kind, port=port, value=value))


def test_switch_reports_the_sites_vlans_and_its_ports(lan, cfg):
    devices = collect_module.collect(cfg)
    sw = switch(devices)
    assert [(v.id, v.name) for v in sw.vlans] == [
        (1, "Default"),
        (10, "Users"),
        (20, "Voice"),
        (30, "Cameras"),
    ]
    assert port_vlans(devices) == {
        "Port 1": (1, [10, 20, 30]),  # "All": native Default, every other network tagged
        "Port 5": (1, [10, 20]),
        "Port 6": (10, []),
        # Port 25 is in a LAG: left empty
    }
    assert all(d.vlans is None for d in devices if d.role == "ap")


def test_overridden_and_odd_profiles_are_left_empty(lan, cfg):
    lan.ports[1]["profileOverrideEnable"] = True
    lan.ports[2]["profileId"] = "pOff"
    lan.ports[0]["profileId"] = "pX"
    lan.profiles.append(
        {"id": "pX", "name": "X", "type": 2, "nativeNetworkId": "n1", "untagNetworkIds": ["n10"]}
    )
    assert port_vlans(collect_module.collect(cfg)) == {}


def test_older_controllers_read_the_v1_networks(lan, cfg):
    lan.v2 = False
    assert [v.id for v in switch(collect_module.collect(cfg)).vlans] == [1, 10, 20, 30]


def test_creates_a_vlan_only_network(lan, cfg):
    devices = run(cfg, "vlan.set", {"id": 40, "name": "IoT"})
    method, version, rel, body = lan.sent[0]
    assert (method, version, rel) == ("POST", "v2", "/lan-networks")
    assert body == {
        "name": "IoT",
        "purpose": 0,
        "vlan": 40,
        "igmpSnoopEnable": False,
        "mldSnoopEnable": False,
    }
    assert (40, "IoT") in [(v.id, v.name) for v in switch(devices).vlans]


def test_renames_a_network_keeping_the_rest(lan, cfg):
    run(cfg, "vlan.set", {"id": 30, "name": "CCTV"})
    [(method, version, rel, body)] = lan.sent
    assert (method, version, rel) == ("PATCH", "v2", "/lan-networks/n30")
    assert body["name"] == "CCTV" and body["gatewaySubnet"] == "192.168.30.1/24"
    assert "id" not in body and "existRA" not in body
    lan.sent.clear()
    run(cfg, "vlan.set", {"id": 30, "name": "CCTV"})  # already so: nothing sent
    assert lan.sent == []


def test_older_controllers_change_through_v1(lan, cfg):
    lan.v2 = False
    run(cfg, "vlan.set", {"id": 40, "name": "IoT"})
    run(cfg, "vlan.set", {"id": 10, "name": "Staff"})
    writes = [(m, v, r) for m, v, r, _ in lan.sent if v == "v1"]
    assert writes == [("POST", "v1", "/lan-networks"), ("PATCH", "v1", "/lan-networks/n10")]


def test_deletes_a_vlan_only_network(lan, cfg):
    devices = run(cfg, "vlan.delete", {"id": 20})
    assert ("DELETE", "v1", "/lan-networks/n20", {}) in lan.sent
    assert 20 not in [v.id for v in switch(devices).vlans]


def test_refuses_to_delete_gateway_and_unknown_networks(lan, cfg):
    with pytest.raises(PluginError, match="gateway network"):
        run(cfg, "vlan.delete", {"id": 30})
    with pytest.raises(PluginError, match="no VLAN 99"):
        run(cfg, "vlan.delete", {"id": 99})
    with pytest.raises(PluginError, match="native network of a switch port profile"):
        run(cfg, "vlan.delete", {"id": 10})  # Port 6's profile
    assert not [s for s in lan.sent if s[0] == "DELETE"][1:]


def test_a_port_reuses_a_profile_with_those_vlans_and_settings(lan, cfg):
    before = json.dumps(lan.profiles)
    devices = run(cfg, "port.vlans", {"untagged": 1, "tagged": [10, 20]}, "Port 6")
    assert lan.sent[-1] == (
        "PUT",
        "v1",
        f"/switches/{SW_PATH}/ports/6/profile",
        {"profileId": "pAP"},
    )
    assert json.dumps(lan.profiles) == before  # no profile made or changed
    assert port_vlans(devices)["Port 6"] == (1, [10, 20])


def test_a_port_gets_a_new_profile_copied_from_its_own(lan, cfg):
    before = [dict(p) for p in lan.profiles]
    devices = run(cfg, "port.vlans", {"untagged": 10, "tagged": [20]}, "Port 6")
    made = lan.profiles[-1]
    assert made["name"] == "Omini 10 20"
    assert (made["nativeNetworkId"], made["tagNetworkIds"], made["untagNetworkIds"]) == (
        "n10",
        ["n20"],
        [],
    )
    assert all(made[k] == BASE[k] for k in vlans_module.SETTINGS if k in BASE)
    assert lan.profiles[:-1] == before  # no existing profile modified
    assert next(p for p in lan.ports if p["port"] == 6)["profileId"] == made["id"]
    assert port_vlans(devices)["Port 6"] == (10, [20])


def test_a_port_keeps_its_poe_setting(lan, cfg):
    lan.ports[2]["profileId"] = "pNoPoe"
    run(cfg, "port.vlans", {"untagged": 20}, "Port 6")
    made = lan.profiles[-1]
    # "Voice" has those VLANs but PoE "do not modify": a new profile with PoE off.
    assert (made["name"], made["poe"], made["nativeNetworkId"]) == ("Omini 20", 1, "n20")


def test_a_taken_name_gets_a_number(lan, cfg):
    lan.profiles.append({"id": "pOld", "name": "Omini 20", "type": 2, "nativeNetworkId": "n1"})
    lan.ports[2]["profileId"] = "pNoPoe"
    run(cfg, "port.vlans", {"untagged": 20, "tagged": None}, "Port 6")
    assert lan.profiles[-1]["name"] == "Omini 20 (2)"


def test_nothing_is_sent_when_the_port_already_has_them(lan, cfg):
    run(cfg, "port.vlans", {"untagged": 1, "tagged": [10, 20]}, "Port 5")
    assert lan.sent == []


def test_an_unapplied_change_is_undone(lan, cfg):
    lan.ignore = True
    count = len(lan.profiles)
    with pytest.raises(PluginError, match="did not apply the VLANs of Port 6"):
        run(cfg, "port.vlans", {"untagged": 10, "tagged": [20]}, "Port 6")
    puts = [b for m, _, r, b in lan.sent if m == "PUT"]
    assert puts[-1] == {"profileId": "pUsers"}  # back to its own profile
    assert len(lan.profiles) == count  # the profile made for it is deleted


def test_port_refusals(lan, cfg):
    def refused(match, value, port, device=SWITCH):
        with pytest.raises(PluginError, match=match):
            run(cfg, "port.vlans", value, port, device)

    refused("in a LAG", {"untagged": 10}, "Port 25")
    lan.ports[0]["profileOverrideEnable"] = True
    refused("Profile Override", {"untagged": 10}, "Port 1")
    refused("native", {"untagged": None, "tagged": [10]}, "Port 6")
    refused("no VLAN 77", {"untagged": 10, "tagged": [77]}, "Port 6")
    refused("has no Port 9", {"untagged": 10}, "Port 9")
    refused("not a switch", {"untagged": 10}, "ETH1", "50:c7:bf:00:00:03")
    assert [s for s in lan.sent if s[0] != "GET"] == []


def test_a_viewer_application_is_told_which_role_it_needs(lan, cfg):

    def forbidden_put(request):
        return fail(-1005, "Operation forbidden.")

    inner = collect_module.Client.keywords["transport"].handler

    def handler(request):
        if request.method == "PUT":
            return forbidden_put(request)
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    with pytest.raises(PluginError, match="Modify permission on Site Settings"):
        run(cfg, "port.vlans", {"untagged": 1, "tagged": [10, 20]}, "Port 6")


def test_an_undo_that_fails_says_so(lan, cfg):
    lan.ignore = True
    inner = collect_module.Client.keywords["transport"].handler
    puts = []

    def handler(request):
        if request.method == "PUT":
            puts.append(1)
            if len(puts) > 1:  # the profile change goes through, putting it back does not
                return fail(-1001, "Invalid request parameters.")
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    with pytest.raises(
        PluginError,
        match=r"did not apply the VLANs of Port 6: .*\(could not be undone: check it in the "
        r"Omada controller\)$",
    ):
        run(cfg, "port.vlans", {"untagged": 10, "tagged": [20]}, "Port 6")
