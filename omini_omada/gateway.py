"""What an Omada gateway serves, from what the controller already answers:
its networks (the site's LAN networks that are gateway interfaces) and the
connected clients it leases addresses to.

A LAN network with ``purpose`` 1 is an interface of the gateway: its
``vlan``, ``gatewaySubnet`` (the gateway's address with the prefix,
"192.168.0.1/24") and ``dhcpSettings`` (``enable``, ``ipaddrStart``,
``ipaddrEnd``). Access (the gateway's ACLs) is not read: it stays unknown.

Fixed addresses (DHCP reservations), pausing a device's internet (a gateway
ACL towards the WAN) and port forwards are not read nor changed: their Open
API endpoints are not identified with enough confidence (see the README)."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from omini_omada.vlans import num, vlan_of

GATEWAY_INTERFACE = 1
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def ipv4(value: Any) -> str | None:
    try:
        return str(ipaddress.IPv4Address(text(value)))
    except ValueError:
        return None


def interfaces(nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for n in nets if num(n.get("purpose")) == GATEWAY_INTERFACE]


def gateway_of(n: dict[str, Any]) -> str | None:
    try:
        return str(ipaddress.IPv4Interface(text(n.get("gatewaySubnet"))))
    except ValueError:
        return None


def dhcp_of(n: dict[str, Any]) -> dict[str, Any] | None:
    d = n.get("dhcpSettings")
    if not isinstance(d, dict) or not isinstance(d.get("enable"), bool):
        return None
    out: dict[str, Any] = {"enabled": d["enable"]}
    if d["enable"]:
        lo, hi = ipv4(d.get("ipaddrStart")), ipv4(d.get("ipaddrEnd"))
        if lo:
            out["from"] = lo
        if hi:
            out["to"] = hi
    return out


def networks(nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The gateway's networks, as Omini's Network fields (VLAN 1 is the LAN)."""
    out: dict[int, dict[str, Any]] = {}
    for n in interfaces(nets):
        vid = vlan_of(n)
        if vid is None or vid in out:
            continue
        x = {
            "vlan": vid,
            "name": text(n.get("name")) or None,
            "gateway": gateway_of(n),
            "dhcp": dhcp_of(n),
        }
        out[vid] = {k: v for k, v in x.items() if v is not None}
    return [out[v] for v in sorted(out)]


def domain(nets: list[dict[str, Any]]) -> str | None:
    """The domain the gateway hands out: the LAN's (VLAN 1), else the first."""
    ordered = sorted(interfaces(nets), key=lambda n: vlan_of(n) or 9999)
    return next((d.lower() for n in ordered if (d := text(n.get("domain")))), None)


def served(nets: list[dict[str, Any]]) -> list[ipaddress.IPv4Network]:
    out = []
    for n in interfaces(nets):
        d = n.get("dhcpSettings")
        if isinstance(d, dict) and d.get("enable") is True:
            try:
                out.append(ipaddress.IPv4Interface(text(n.get("gatewaySubnet"))).network)
            except ValueError:
                continue
    return out


def norm_mac(value: Any) -> str:
    v = text(value).lower().replace("-", ":")
    return v if MAC.match(v) else ""


def leases(clients: list[dict[str, Any]], nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Connected clients with an address in a network the gateway serves
    DHCP on (the client list gives no lease expiry)."""
    subnets = served(nets)
    out = []
    for c in clients:
        m, ip = norm_mac(c.get("mac")), ipv4(c.get("ip"))
        if c.get("active") is False or not m or not ip:
            continue
        if not any(ipaddress.IPv4Address(ip) in s for s in subnets):
            continue
        lease = {"ip": ip, "mac": m, "hostname": text(c.get("hostName")) or None}
        out.append({k: v for k, v in lease.items() if v})
    return out
