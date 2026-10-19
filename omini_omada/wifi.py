"""The site's Wi-Fi networks (SSIDs of its WLAN groups, Open API
``wireless-network``) and the VLAN each one puts its clients in, read every
round and changed by wifi.set and wifi.delete.

An SSID's VLAN is its ``vlanSetting`` (mode 1: a custom VLAN id; mode 0: the
untagged LAN); older controllers answer ``vlanEnable`` / ``vlanId``. New SSIDs
are WPA2 personal (``security`` 3, ``versionPsk`` 2), on both bands, in the
site's first WLAN group. The password goes to the controller only."""

from __future__ import annotations

import time
from typing import Any

from omini_sdk import PluginError
from omini_sdk.changes import apply_verify

from omini_omada.client import Client

# How long a change waits for the controller to show it (the SDK's window).
VERIFY_S = 30
POLL_S = 2

# 0 none, 3 WPA-Personal. 2 (WPA-Enterprise) has no value in the data model:
# it and any other code are left empty.
SECURITY = {0: "open", 3: "wpa2"}


def num(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def vlan_of(ssid: dict[str, Any]) -> int | None:
    vs = ssid.get("vlanSetting")
    if isinstance(vs, dict):
        if num(vs.get("mode")) == 1:
            return num((vs.get("customConfig") or {}).get("vlanId"))
        return None
    if ssid.get("vlanEnable"):
        return num(ssid.get("vlanId"))
    return None


def security_of(ssid: dict[str, Any]) -> str | None:
    psk = ssid.get("pskSetting") if isinstance(ssid.get("pskSetting"), dict) else {}
    version = num(psk.get("versionPsk"))
    if num(ssid.get("security")) == 3 and version in (3, 4):
        return "wpa3" if version == 4 else "wpa2wpa3"
    return SECURITY.get(num(ssid.get("security")) or 0)


def wlan_groups(c: Client, sid: str) -> list[dict[str, Any]]:
    out = c.pages(f"sites/{sid}/wireless-network/wlans")
    if not out:
        result = c.get(f"sites/{sid}/wireless-network/wlans")
        out = [r for r in (result if isinstance(result, list) else []) if isinstance(r, dict)]
    return out


def ssids(c: Client, sid: str) -> list[tuple[str, dict[str, Any]]]:
    """(WLAN group id, SSID) of every SSID of the site."""
    out = []
    for g in wlan_groups(c, sid):
        wid = str(g.get("wlanId") or g.get("id") or "")
        if not wid:
            continue
        for s in c.pages(f"sites/{sid}/wireless-network/wlans/{wid}/ssids"):
            out.append((wid, s))
    return out


def wifi_networks(rows: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for _wid, s in rows:
        name = str(s.get("name") or "").strip()
        if not name or name in out:
            continue
        vid = vlan_of(s)
        out[name] = {
            "ssid": name,
            "vlan": vid if vid and vid != 1 else None,
            "security": security_of(s),
            "enabled": s.get("ssidEnable") is not False,
        }
    return list(out.values())


def vlan_setting(vid: int) -> dict[str, Any]:
    if vid == 1:
        return {"mode": 0}
    return {"mode": 1, "customConfig": {"vlanId": vid}}


def psk(password: str) -> dict[str, Any]:
    return {
        "securityKey": password,
        "encryptionPsk": 3,
        "versionPsk": 2,
        "gikRekeyPskEnable": False,
    }


def named(rows: list[tuple[str, dict[str, Any]]], name: str) -> tuple[str, dict[str, Any]] | None:
    """The one SSID called ``name``. Omini shows one Wi-Fi network per name, so
    a name used in several WLAN groups is refused rather than picking one."""
    hits = [(wid, s) for wid, s in rows if name and str(s.get("name") or "") == name]
    if len(hits) > 1:
        raise PluginError(
            f'the site has a Wi-Fi network "{name}" in several WLAN groups: '
            "rename or remove the others in the controller first"
        )
    return hits[0] if hits else None


def set_wifi(c: Client, sid: str, want: dict[str, Any]) -> None:
    """Sends the SSID's change, then reads the site again until it has it."""
    wait_for(c, lambda: send_wifi(c, sid, want), lambda: shown(c, sid, want))


def send_wifi(c: Client, sid: str, want: dict[str, Any]) -> None:
    rows = ssids(c, sid)
    found = named(rows, str(want.get("previous") or "")) or named(rows, str(want["ssid"]))
    if found is None:
        if not want.get("password"):
            raise PluginError("a new Wi-Fi network needs a password")
        groups = wlan_groups(c, sid)
        if not groups:
            raise PluginError("the site has no WLAN group to add the Wi-Fi network to")
        wid = str(groups[0].get("wlanId") or groups[0].get("id"))
        c.post(
            f"sites/{sid}/wireless-network/wlans/{wid}/ssids",
            {
                "name": str(want["ssid"]),
                "deviceType": 1,
                "band": 3,
                "guestNetEnable": False,
                "security": 3,
                "broadcast": True,
                "vlanSetting": vlan_setting(int(want.get("vlan") or 1)),
                "pskSetting": psk(str(want["password"])),
                "mloEnable": False,
                "pmfMode": 3,
                "enable11r": False,
            },
        )
        if want.get("enabled") is False:
            created = next(
                (s for w, s in ssids(c, sid) if w == wid and s.get("name") == want["ssid"]), None
            )
            if created:
                enable(c, sid, created, False)
        return
    wid, s = found
    body = {
        "name": str(want["ssid"]),
        "band": s.get("band", 3),
        "guestNetEnable": bool(s.get("guestNetEnable")),
        "security": s.get("security", 3),
        "broadcast": s.get("broadcast", True),
        "vlanSetting": vlan_setting(int(want["vlan"]))
        if want.get("vlan") is not None
        else (s.get("vlanSetting") or {"mode": 0}),
    }
    if want.get("password"):
        body["security"] = 3
        body["pskSetting"] = psk(str(want["password"]))
    c.patch(
        f"sites/{sid}/wireless-network/wlans/{wid}/ssids/{s['ssidId']}/update-basic-config", body
    )
    if want.get("enabled") is not None and (s.get("ssidEnable") is not False) != bool(
        want["enabled"]
    ):
        enable(c, sid, s, bool(want["enabled"]))


def enable(c: Client, sid: str, s: dict[str, Any], on: bool) -> None:
    c.patch(f"sites/{sid}/wireless-network/ssids/{s['ssidId']}/enable", {"ssidEnable": on})


def delete_wifi(c: Client, sid: str, ssid: str) -> None:
    found = named(ssids(c, sid), ssid)
    if found is None:
        raise PluginError(f'the site has no Wi-Fi network "{ssid}"')
    wid, s = found
    wait_for(
        c,
        lambda: c.delete(f"sites/{sid}/wireless-network/wlans/{wid}/ssids/{s['ssidId']}"),
        lambda: not any(n["ssid"] == ssid for n in wifi_networks(ssids(c, sid))),
    )


def shown(c: Client, sid: str, want: dict[str, Any]) -> bool:
    """Whether the site has the SSID as asked (its VLAN and on/off)."""
    n = next((n for n in wifi_networks(ssids(c, sid)) if n["ssid"] == want["ssid"]), None)
    return bool(
        n
        and (want.get("vlan") is None or (n["vlan"] or 1) == int(want["vlan"]))
        and (want.get("enabled") is None or n["enabled"] == bool(want["enabled"]))
    )


def wait_for(c: Client, send: Any, verify: Any) -> None:
    """Sends a change and reads the site again (a read that fails meanwhile is
    read again) until it shows it. A Wi-Fi change is not undone: a changed
    password cannot be put back (the controller never shows it)."""
    apply_verify(
        send,
        verify,
        timeout=VERIFY_S,
        interval=POLL_S,
        message="the controller did not apply the Wi-Fi change",
        sleep=time.sleep,
    )
