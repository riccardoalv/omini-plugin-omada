"""An access point's Wi-Fi radios and the channel each is on.

Two answers of the Open API describe them, one block per radio (2.4 GHz,
5 GHz, a second 5 GHz, 6 GHz):

- ``GET .../aps/{apMac}/radios`` (``wp2g``, ``wp5g``, ``wp5g2``, ``wp6g``):
  what the radio is doing now, ``actualChannel``, ``bandWidth`` ("20MHz")
  and ``txPower`` (dBm);
- ``GET .../aps/{apMac}`` (``radioSetting2g``, ``radioSetting5g``,
  ``radioSetting5g2``, ``radioSetting6g``): its settings in the controller,
  ``radioEnable`` and ``channel`` (0: the controller picks it).

A radio is named as the access point's wireless interfaces are ("2.4 GHz",
"5 GHz", "5 GHz-2", "6 GHz"): what a wifi.channel change names."""

from __future__ import annotations

import re
import time
from typing import Any

from omini_sdk import PluginError

from omini_omada.client import Client, Unavailable

# (Omini's name, band, live block, settings block)
RADIOS = [
    ("2.4 GHz", "2.4ghz", "wp2g", "radioSetting2g"),
    ("5 GHz", "5ghz", "wp5g", "radioSetting5g"),
    ("5 GHz-2", "5ghz", "wp5g2", "radioSetting5g2"),
    ("6 GHz", "6ghz", "wp6g", "radioSetting6g"),
]
WIDTHS = {20, 40, 80, 160, 320}


def num(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def channel_of(value: Any) -> int | None:
    n = num(value)
    return n if n is not None and 1 <= n <= 233 else None


def width_of(value: Any) -> int | None:
    """ "20MHz", "80" → MHz; "20/40MHz" (automatic) and anything else → None."""
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        return None
    hit = re.fullmatch(r"\s*(\d+)\s*(MHz)?\s*", str(value), re.IGNORECASE)
    n = int(hit.group(1)) if hit else None
    return n if n in WIDTHS else None


def power_of(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def auto_of(setting: dict[str, Any]) -> bool | None:
    if "channel" not in setting:
        return None
    n = num(setting.get("channel"))
    if n == 0:
        return True
    return False if n is not None and n > 0 else None


def radios(info: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    """Each radio the access point has, as Omini's Radio fields."""
    out = []
    for name, band, live_key, setting_key in RADIOS:
        now = live.get(live_key) if isinstance(live.get(live_key), dict) else None
        setting = info.get(setting_key) if isinstance(info.get(setting_key), dict) else None
        if now is None and setting is None:
            continue
        now, setting = now or {}, setting or {}
        enabled = setting.get("radioEnable")
        radio = {
            "name": name,
            "band": band,
            "channel": channel_of(now.get("actualChannel")) or channel_of(setting.get("channel")),
            "width_mhz": width_of(now.get("bandWidth")),
            "auto": auto_of(setting),
            "tx_power_dbm": power_of(now.get("txPower")),
            "enabled": enabled if isinstance(enabled, bool) else None,
        }
        out.append({k: v for k, v in radio.items() if v is not None})
    return out


def wireless_uplink(info: dict[str, Any]) -> bool | None:
    """A mesh access point (it reaches the network over Wi-Fi)."""
    linked = info.get("wirelessLinked")
    return linked if isinstance(linked, bool) else None


# --- wifi.channel (not declared: see the README) -------------------------------
#
# The Open API's AP radio settings ("radio-config") are not verified on a
# controller yet, so plugin.yaml does not declare wifi.channel and Omini never
# asks for it. The change is kept here, tested against the shape below, for
# when it is: PATCH .../aps/{apMac}/radio-config with the radio's settings
# block, only its channel changed, read back until the radio is on it.

RADIO_CONFIG = "radio-config"
VERIFY_READS = 20
PAUSE_S = 3.0

CHANNELS = {
    "2.4ghz": set(range(1, 14)),
    "5ghz": {*range(36, 65, 4), *range(100, 145, 4), *range(149, 178, 4)},
    "6ghz": set(range(1, 234, 4)),
}


def set_channel_unverified(c: Client, sid: str, ap: str, value: Any) -> None:
    """Moves one radio of access point ``ap`` (API MAC, AA-BB-...) to a
    channel; undone when the radio does not report it."""
    name = value.get("radio") if isinstance(value, dict) else None
    channel = value.get("channel") if isinstance(value, dict) else None
    if isinstance(channel, bool) or not isinstance(channel, int):
        raise PluginError(f"the channel must be a number, not {channel!r}")
    spec = next((r for r in RADIOS if r[0] == name), None)
    if spec is None:
        raise PluginError(f"the access point has no radio {name!r}")
    _, band, live_key, setting_key = spec
    path = f"sites/{sid}/aps/{ap}"
    info = c.get(path)
    info = info if isinstance(info, dict) else {}
    if info.get("wirelessLinked") is True:
        raise PluginError(
            "the access point reaches the network over Wi-Fi (mesh): a new channel could cut it "
            "off, so Omini does not move it"
        )
    before = info.get(setting_key)
    if not isinstance(before, dict):
        raise PluginError(f"the access point has no radio {name}")
    if channel not in CHANNELS[band]:
        raise PluginError(f"channel {channel} is not a channel of the {name} radio")
    if num(before.get("channel")) == channel:
        return
    after = {
        **before,
        "channel": str(channel) if isinstance(before.get("channel"), str) else channel,
    }
    c.patch(f"{path}/{RADIO_CONFIG}", {setting_key: after})
    for i in range(VERIFY_READS):
        if i:
            time.sleep(PAUSE_S)
        try:
            setting = (c.get(path) or {}).get(setting_key) or {}
            live = (c.get(f"{path}/radios") or {}).get(live_key) or {}
        except Unavailable:
            continue
        if num(setting.get("channel")) != channel:
            break  # the controller did not keep it
        if channel_of(live.get("actualChannel")) == channel:
            return
    try:
        c.patch(f"{path}/{RADIO_CONFIG}", {setting_key: before})
        undone = "undone on the controller"
    except Unavailable as e:
        undone = f"not undone ({e}): check the radio in the controller"
    raise PluginError(f"the radio did not move to channel {channel}; the change was {undone}")
