"""The site's VLANs (its LAN networks) and the VLANs of each switch port (its
port profile), read every round and changed by vlan.set, vlan.delete and
port.vlans.

Omada keeps VLANs per site, not per switch: a LAN network is a VLAN-only one
(``purpose`` 0) or a gateway interface (``purpose`` 1, with its own subnet and
DHCP). Every switch of the site carries all of them, so each switch reports
the site's networks as its VLANs.

A switch port gets its VLANs from its port profile (LAN profile): a native
network (untagged, the PVID), tagged networks and extra untagged networks.
Ports in a LAG, ports whose profile is overridden on the port ("Profile
Override") and ports whose profile has extra untagged networks cannot be
described as Omini's untagged + tagged, so their VLANs are left empty.

A port's VLANs change by giving it another profile ("Set profile for given
port"): a profile with exactly those VLANs and the same other settings (PoE,
802.1X, isolation, LLDP-MED, rate limits, STP, loopback detection...) as the
port's current one, or a new one named "Omini <native> <tagged>" copied from
the current one with the VLANs replaced. No existing profile is ever modified:
other ports use them."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any, TypeVar

from omini_sdk import PluginError, PortVlans, Vlan

from omini_omada.client import Client, Forbidden, NotFound, Unavailable

T = TypeVar("T")

# Profile type: 0 the built-in "All", 1 the built-in "Disable", 2 the others.
ALL, DISABLE = 0, 1
# LAN network purpose: 0 VLAN only, 1 gateway interface. vlanType 1: several VLANs.
VLAN_ONLY = 0
MULTIPLE = 1
# The settings of a profile besides its VLANs (the body of "Create new LAN
# profile"): kept as they are when a port changes profile.
SETTINGS = (
    "poe",
    "dot1x",
    "portIsolationEnable",
    "lldpMedEnable",
    "bandWidthCtrlType",
    "stormCtrl",
    "bandCtrl",
    "spanningTreeEnable",
    "loopbackDetectEnable",
    "eeeEnable",
    "flowControlEnable",
    "loopbackDetectVlanBasedEnable",
    "dhcpL2RelaySettings",
)
# Fields of a LAN network that are read only (status, not configuration).
READ_ONLY = {"id", "portal", "portalId", "portalName", "accessControlRule", "rateLimit", "primary"}
VERIFY_READS = 5
PAUSE_S = 1.0
NAME_MAX = 128


def num(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def api_mac(m: str) -> str:
    return m.replace(":", "-").upper()


def newer_or_older(v2: Callable[[], T], v1: Callable[[], T]) -> T:
    """The v2 endpoint where the controller has it (5.15 and later), else the
    v1 one (deprecated since, still answered)."""
    try:
        return v2()
    except NotFound:
        return v1()


# -- reading --------------------------------------------------------------


def networks(c: Client, sid: str) -> list[dict[str, Any]]:
    path = f"sites/{sid}/lan-networks"
    return newer_or_older(lambda: c.pages(path, api="v2"), lambda: c.pages(path))


def profiles(c: Client, sid: str) -> list[dict[str, Any]]:
    return c.pages(f"sites/{sid}/lan-profiles")


def switch_ports(c: Client, sid: str, m: str) -> list[dict[str, Any]]:
    """The switch's ports with their profile ("Get switch info", portList)."""
    info = c.get(f"sites/{sid}/switches/{api_mac(m)}")
    ports = info.get("portList") if isinstance(info, dict) else None
    return [p for p in ports or [] if isinstance(p, dict)]


def vlan_of(net: dict[str, Any]) -> int | None:
    """The VLAN of a single-VLAN network (multi-VLAN interfaces have none)."""
    if num(net.get("vlanType")) == MULTIPLE:
        return None
    vid = num(net.get("vlan"))
    return vid if vid is not None and 1 <= vid <= 4094 else None


def site_vlans(nets: list[dict[str, Any]]) -> list[Vlan]:
    out: dict[int, Vlan] = {}
    for n in nets:
        vid = vlan_of(n)
        if vid is not None and vid not in out:
            name = n.get("name")
            out[vid] = Vlan(id=vid, name=name.strip() if isinstance(name, str) and name else None)
    return [out[k] for k in sorted(out)]


def profile_vlans(
    p: dict[str, Any], nets: list[dict[str, Any]]
) -> tuple[int, frozenset[int]] | None:
    """(native VLAN, tagged VLANs) of a profile, or None when it does not fit
    Omini's model (Disable, extra untagged networks, unknown networks)."""
    vid = {str(n.get("id")): vlan_of(n) for n in nets if n.get("id")}
    kind = num(p.get("type"))
    if kind == DISABLE:
        return None
    native_id = str(p.get("nativeNetworkId") or "")
    native = vid.get(native_id)
    if native is None:
        return None
    untagged = [str(x) for x in p.get("untagNetworkIds") or [] if str(x) != native_id]
    if untagged:
        return None
    tag_ids = [str(x) for x in p.get("tagNetworkIds") or []]
    if kind == ALL and not tag_ids:
        # "All": the native network untagged, every other network tagged.
        tags = {v for v in vid.values() if v is not None and v != native}
    else:
        if any(vid.get(t) is None for t in tag_ids):
            return None
        tags = {vid[t] for t in tag_ids} - {native}  # type: ignore[misc]
    return native, frozenset(tags)


def port_vlans(
    port: dict[str, Any], profs: dict[str, dict[str, Any]], nets: list[dict[str, Any]]
) -> tuple[int, frozenset[int]] | None:
    if port.get("lagPort") is True or port.get("profileOverrideEnable") is True:
        return None
    p = profs.get(str(port.get("profileId") or ""))
    return profile_vlans(p, nets) if p else None


def to_model(v: tuple[int, frozenset[int]]) -> PortVlans:
    return PortVlans(untagged=v[0], tagged=sorted(v[1]) or None)


def read_switch(
    c: Client, sid: str, m: str, nets: list[dict[str, Any]], profs: list[dict[str, Any]]
) -> dict[int, PortVlans]:
    """The VLANs of every port of the switch that Omini can change."""
    by_id = {str(p.get("id")): p for p in profs if p.get("id")}
    out: dict[int, PortVlans] = {}
    for port in switch_ports(c, sid, m):
        pid = num(port.get("port"))
        v = port_vlans(port, by_id, nets)
        if pid is not None and v is not None:
            out[pid] = to_model(v)
    return out


# -- vlan.set / vlan.delete ------------------------------------------------


def find_net(nets: list[dict[str, Any]], vid: int) -> dict[str, Any] | None:
    return next((n for n in nets if vlan_of(n) == vid), None)


def set_vlan(c: Client, sid: str, vid: int, name: str) -> None:
    if not 1 <= vid <= 4090:
        raise PluginError("an Omada VLAN must be between 1 and 4090")
    name = name.strip()
    if not name:
        raise PluginError("the VLAN needs a name")
    path = f"sites/{sid}/lan-networks"
    net = find_net(networks(c, sid), vid)
    if net is None:
        body = {
            "name": name,
            "purpose": VLAN_ONLY,
            "vlan": vid,
            "igmpSnoopEnable": False,
            "mldSnoopEnable": False,
        }
        newer_or_older(lambda: c.post(path, body, api="v2"), lambda: c.post(path, body))
    elif net.get("name") != name:
        # The network as read, with the new name: nothing else changes.
        body = {
            k: v
            for k, v in net.items()
            if k not in READ_ONLY and not k.startswith(("exist", "support"))
        }
        body["name"] = name
        nid = net["id"]
        newer_or_older(
            lambda: c.patch(f"{path}/{nid}", body, api="v2"),
            lambda: c.patch(f"{path}/{nid}", body),
        )
    else:
        return
    wait_for(
        lambda: (n := find_net(networks(c, sid), vid)) is not None and n.get("name") == name,
        f"the controller did not apply VLAN {vid}",
    )


def delete_vlan(c: Client, sid: str, vid: int) -> None:
    net = find_net(networks(c, sid), vid)
    if net is None:
        raise PluginError(f"the site has no VLAN {vid}")
    if num(net.get("purpose")) != VLAN_ONLY:
        raise PluginError(
            f'VLAN {vid} ("{net.get("name")}") is a gateway network (an interface with its own '
            "subnet and DHCP), not a VLAN-only one: Omini only deletes VLAN-only networks. "
            "Delete it in the controller (Settings → Wired Networks → LAN) if you mean to"
        )
    try:
        c.delete(f"sites/{sid}/lan-networks/{net['id']}")
    except Forbidden:
        raise
    except Unavailable as e:
        if "-33566" in str(e):
            raise PluginError(
                f"VLAN {vid} is the native network of a switch port profile in use: "
                "move those ports to another VLAN first"
            ) from e
        raise
    wait_for(
        lambda: find_net(networks(c, sid), vid) is None,
        f"the controller did not delete VLAN {vid}",
    )


# -- port.vlans --------------------------------------------------------------


def wanted(value: dict[str, Any]) -> tuple[int, frozenset[int]]:
    untagged = num(value.get("untagged"))
    tagged = frozenset(n for x in value.get("tagged") or [] if (n := num(x)) is not None)
    if untagged is None:
        raise PluginError(
            "an Omada switch port always has a native (untagged) network: choose one for it"
        )
    return untagged, tagged - {untagged}


def profile_name(want: tuple[int, frozenset[int]], taken: set[str]) -> str:
    base = f"Omini {want[0]}"
    if want[1]:
        base += " " + ",".join(str(v) for v in sorted(want[1]))
    if len(base) > NAME_MAX - 6:
        base = base[: NAME_MAX - 7] + "…"
    name, i = base, 2
    while name in taken:
        name, i = f"{base} ({i})", i + 1
    return name


def same_settings(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in SETTINGS)


def set_port(c: Client, sid: str, m: str, pid: int, value: dict[str, Any], label: str) -> None:
    want = wanted(value)
    port = next((p for p in switch_ports(c, sid, m) if num(p.get("port")) == pid), None)
    if port is None:
        raise PluginError(f"the switch has no {label}")
    if port.get("lagPort") is True:
        raise PluginError(f"{label} is in a LAG: change the LAG's profile in the controller")
    if port.get("profileOverrideEnable") is True:
        raise PluginError(
            f"{label} has Profile Override on in the controller (its own settings win over "
            "profiles): turn it off in the switch's Ports tab first"
        )
    nets = networks(c, sid)
    ids = {vid: str(n["id"]) for n in nets if (vid := vlan_of(n)) is not None and n.get("id")}
    missing = sorted(v for v in {want[0], *want[1]} if v not in ids)
    if missing:
        raise PluginError(f"the site has no VLAN {', '.join(map(str, missing))}")
    profs = profiles(c, sid)
    by_id = {str(p.get("id")): p for p in profs if p.get("id")}
    old_id = str(port.get("profileId") or "")
    current = by_id.get(old_id)
    if current is None:
        raise PluginError(f"cannot read the profile of {label}: nothing was changed")
    if port_vlans(port, by_id, nets) == want:
        return
    target = next(
        (
            str(p["id"])
            for p in profs
            if p.get("id") and profile_vlans(p, nets) == want and same_settings(p, current)
        ),
        None,
    )
    created = None
    if target is None:
        name = profile_name(want, {str(p.get("name")) for p in profs})
        body: dict[str, Any] = {k: current[k] for k in SETTINGS if k in current}
        body.update(
            name=name,
            nativeNetworkId=ids[want[0]],
            tagNetworkIds=[ids[v] for v in sorted(want[1])],
            untagNetworkIds=[],
        )
        voice = current.get("voiceNetworkId")
        if voice and voice in (body["nativeNetworkId"], *body["tagNetworkIds"]):
            body["voiceNetworkId"] = voice
        result = c.post(f"sites/{sid}/lan-profiles", body)
        created = str(result.get("id") or "") if isinstance(result, dict) else ""
        if not created:
            created = next(
                (str(p["id"]) for p in profiles(c, sid) if p.get("name") == name and p.get("id")),
                "",
            )
        if not created:
            raise PluginError(f'the controller did not create the profile "{name}"')
        target = created
    path = f"sites/{sid}/switches/{api_mac(m)}/ports/{pid}/profile"
    c.put(path, {"profileId": target})

    def done() -> bool:
        p = next((p for p in switch_ports(c, sid, m) if num(p.get("port")) == pid), None)
        if p is None or str(p.get("profileId")) != target:
            return False
        by = {str(x.get("id")): x for x in profiles(c, sid) if x.get("id")}
        return port_vlans(p, by, networks(c, sid)) == want

    try:
        wait_for(done, f"the controller did not apply the VLANs of {label}")
    except PluginError:
        # Back to the profile it had; the profile made for it goes away.
        with contextlib.suppress(Unavailable):
            c.put(path, {"profileId": old_id})
        if created:
            with contextlib.suppress(Unavailable):
                c.delete(f"sites/{sid}/lan-profiles/{created}")
        raise


def wait_for(check: Callable[[], bool], failure: str) -> None:
    for i in range(VERIFY_READS):
        if i:
            time.sleep(PAUSE_S)
        try:
            if check():
                return
        except NotFound:
            break
        except Unavailable:
            continue
    raise PluginError(failure)
