"""The gateway's networks and DHCP clients, from the site's LAN networks and
its client list."""

import pytest

from omini_omada.collect import collect
from tests.conftest import OMADAC_ID, SITE

GATEWAY = "50:c7:bf:00:00:01"

# Get LAN network list (v1: the v2 path is missing on this controller).
LAN_NETWORKS = [
    {
        "id": "n1",
        "name": "LAN",
        "purpose": 1,
        "vlanType": 0,
        "vlan": 1,
        "gatewaySubnet": "192.168.0.1/24",
        "domain": "Home.Arpa",
        "dhcpSettings": {
            "enable": True,
            "ipaddrStart": "192.168.0.100",
            "ipaddrEnd": "192.168.0.199",
            "leasetime": 120,
            "dhcpns": "auto",
        },
        "igmpSnoopEnable": False,
        "primary": True,
    },
    {"id": "n10", "name": "Users", "purpose": 0, "vlan": 10, "igmpSnoopEnable": True},
    {
        "id": "n20",
        "name": "IOT",
        "purpose": 1,
        "vlanType": 0,
        "vlan": 20,
        "gatewaySubnet": "192.168.20.1/24",
        "dhcpSettings": {"enable": False},
    },
    {
        "id": "n30",
        "name": "Trunk",
        "purpose": 1,
        "vlanType": 1,
        "vlans": "30-40",
        "gatewaySubnet": "192.168.30.1/24",
    },
]


@pytest.fixture
def site(omada):
    omada.routes[f"/openapi/v1/{OMADAC_ID}/sites/{SITE}/lan-networks"] = {
        "errorCode": 0,
        "msg": "Success.",
        "result": {
            "totalRows": len(LAN_NETWORKS),
            "currentPage": 1,
            "currentSize": 1000,
            "data": LAN_NETWORKS,
        },
    }
    return omada


def gateway(devices):
    return next(d for d in devices if d.key == GATEWAY)


def test_reads_the_gateway_s_networks(site, cfg):
    gw = gateway(collect(cfg))
    # Gateway interfaces only (a VLAN-only network is the switches'; a
    # multi-VLAN interface has no single VLAN); access stays unknown.
    assert [n.model_dump(exclude_none=True, by_alias=True) for n in gw.networks] == [
        {
            "vlan": 1,
            "name": "LAN",
            "gateway": "192.168.0.1/24",
            "dhcp": {"enabled": True, "from": "192.168.0.100", "to": "192.168.0.199"},
        },
        {"vlan": 20, "name": "IOT", "gateway": "192.168.20.1/24", "dhcp": {"enabled": False}},
    ]
    assert gw.dns_domain == "home.arpa"


def test_reads_the_dhcp_clients(site, cfg):
    gw = gateway(collect(cfg))
    # Online clients in a network with DHCP on: not the IOT network's (DHCP
    # off) nor the offline laptop.
    assert [x.model_dump(exclude_none=True) for x in gw.dhcp_leases] == [
        {"ip": "192.168.0.101", "mac": "aa:bb:cc:dd:ee:01", "hostname": "pixel-8"},
        {"ip": "192.168.0.10", "mac": "00:11:32:aa:bb:cc", "hostname": "nas"},
        {"ip": "192.168.0.30", "mac": "aa:bb:cc:dd:ee:04"},
    ]


def test_without_lan_networks_the_gateway_has_none(omada, cfg):
    gw = gateway(collect(cfg))
    assert (gw.networks, gw.dns_domain, gw.dhcp_leases) == (None, None, None)


def test_only_the_gateway_serves_networks(site, cfg):
    assert all(d.networks is None for d in collect(cfg) if d.key != GATEWAY)


def test_declares_no_gateway_change():
    from pathlib import Path

    manifest = (Path(__file__).parent.parent / "plugin.yaml").read_text()
    for kind in ("network.set", "reservation.set", "pause.set", "dns.set"):
        assert kind not in manifest
