"""Configuration backups: each device's settings as sorted JSON.

The Open API has no text export of a device, so the backup is built from what
the controller keeps: each device's own settings (its name and, for a switch,
each port's configuration from "Get switch info"; a gateway's port settings;
an access point's settings) and, on one device of the site, the site's
settings: its LAN networks, port profiles and Wi-Fi networks (SSIDs). Live
status (uptime, counters, link state, clients) and secrets (Wi-Fi passwords,
RADIUS and PPPoE secrets, any field named like a password, secret or key) are left
out, and every list is sorted, so two backups of an unchanged site are equal.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from omini_sdk import ConfigBackup, PluginError, log, optional

from omini_omada import vlans as vl
from omini_omada import wifi as wlan
from omini_omada.client import Client, Forbidden, HttpError, NotFound

MISSING: Any = object()  # a site setting left out of the backup

# The device list's fields that are settings (the rest is status).
DEVICE_KEYS = ("mac", "name", "type", "model", "modelName")
# Settings of "Get switch info", "Get gateway info" and "Get AP info"; their
# other fields are status (uptime, CPU, link state, counters, clients...).
INFO_KEYS = (
    "portList",
    "portConfigs",
    "ipSetting",
    "ledSetting",
    "lldpEnable",
    "loopbackDetectEnable",
    "stpSetting",
    "jumbo",
    "snmp",
    "location",
    "wlanId",
    "radioSetting2g",
    "radioSetting5g",
    "radioSetting5g2",
    "radioSetting6g",
    "mvlanEnable",
    "mvlanSetting",
    "lanPortSettings",
)
# Status fields inside a port's settings (a gateway's "linkSpeed" is the
# speed it is set to: kept).
PORT_LIVE = {
    "linkStatus",
    "portStatus",
    "tx",
    "rx",
    "total",
    "txRate",
    "rxRate",
    "poePower",
    "downlink",
    "online",
}
# Status and bookkeeping fields of the site's records.
SITE_LIVE = {"createTime", "createdTime", "modifyTime", "updateTime", "lastSeen", "timestamp"}

SECRET_WORDS = ("password", "passphrase", "secret", "securitykey", "preshared", "privatekey", "pwd")

PATHS = {"switch": "switches", "gateway": "gateways", "ap": "aps"}
ORDER = ("gateway", "switch", "ap")


def secret(key: str) -> bool:
    k = key.lower().replace("_", "")
    return any(w in k for w in SECRET_WORDS)


def clean(value: Any, live: set[str] | frozenset[str] = frozenset()) -> Any:
    """``value`` without secrets nor live fields, at every depth."""
    if isinstance(value, dict):
        return {
            k: clean(v, live)
            for k, v in value.items()
            if isinstance(k, str) and not secret(k) and k not in live
        }
    if isinstance(value, list):
        return [clean(v, live) for v in value]
    return value


def ordered(rows: list[Any], *keys: str) -> list[dict[str, Any]]:
    """Records in a stable order (the controller may answer them in any order)."""
    dicts = [r for r in rows if isinstance(r, dict)]
    return sorted(dicts, key=lambda r: tuple(str(r.get(k) or "") for k in keys))


def api_mac(m: str) -> str:
    return m.replace(":", "-").upper()


def device_settings(c: Client, sid: str, row: dict[str, Any], m: str) -> dict[str, Any]:
    kind = str(row.get("type") or "").lower()
    out = {k: row[k] for k in DEVICE_KEYS if row.get(k) is not None}
    path = PATHS.get(kind)
    if path:
        try:
            info = c.get(f"sites/{sid}/{path}/{api_mac(m)}")
        except (Forbidden, NotFound) as e:
            # Left out the same way every time (role or version).
            log.info("%s settings left out of the backup: %s", kind, e)
            info = None
        except HttpError as e:
            # Never a half-read backup: it would differ from the last one.
            raise PluginError(f"cannot read the settings of {m}: {e}") from e
        if isinstance(info, dict):
            for k in INFO_KEYS:
                if info.get(k) is not None:
                    out[k] = info[k]
    for k in ("portList", "portConfigs"):
        if isinstance(out.get(k), list):
            out[k] = [
                {p: v for p, v in port.items() if p not in PORT_LIVE}
                for port in sorted(
                    (p for p in out[k] if isinstance(p, dict)),
                    key=lambda p: p.get("port") if isinstance(p.get("port"), int) else 0,
                )
            ]
    out["mac"] = m
    return clean(out)


def site_settings(c: Client, sid: str) -> dict[str, Any]:
    """The site's LAN networks, port profiles and SSIDs; a read the
    application's role or the version cannot do, or the controller fails to
    answer, is left out."""
    out: dict[str, Any] = {}
    reads: tuple[tuple[str, Callable[[], Any]], ...] = (
        ("networks", lambda: ordered(vl.networks(c, sid), "name", "id")),
        ("port_profiles", lambda: ordered(vl.profiles(c, sid), "name", "id")),
        (
            "wifi_networks",
            lambda: ordered(
                [{**s, "wlanId": wid} for wid, s in wlan.ssids(c, sid)], "wlanId", "name", "ssidId"
            ),
        ),
    )
    for name, read in reads:
        # An optional part (as in the UniFi plugin): one the role or version
        # lacks, or the controller fails to answer, is left out (logged); the
        # devices' own settings are still backed up.
        value = optional(f"{name} (backup)", read, MISSING)
        if value is not MISSING:
            out[name] = clean(value, SITE_LIVE)
    return out


def anchor(kinds: dict[str, str]) -> str | None:
    """The device whose backup holds the site's settings: the gateway, else
    the switch, else the access point with the lowest MAC (stable from one
    backup to the next)."""
    for kind in ORDER:
        keys = sorted(m for m, k in kinds.items() if k == kind)
        if keys:
            return keys[0]
    return min(kinds) if kinds else None


def backups(c: Client, sid: str, rows: dict[str, dict[str, Any]]) -> list[ConfigBackup]:
    """One backup per device collect reports (keyed by its MAC)."""
    if not rows:
        raise PluginError("the site has no connected device to back up")
    home = anchor({m: str(r.get("type") or "").lower() for m, r in rows.items()})
    out = []
    for m in sorted(rows):
        content: dict[str, Any] = {"device": device_settings(c, sid, rows[m], m)}
        if m == home:
            content["site"] = site_settings(c, sid)
        out.append(
            ConfigBackup(
                device=m, format="json", content=json.dumps(content, sort_keys=True, indent=2)
            )
        )
    return out
