"""Maps the Omada Open API to Omini devices: one Device per gateway, switch and
access point of the site, keyed by its MAC."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from omini_sdk import (
    Config,
    Device,
    FdbEntry,
    Firmware,
    Gateway,
    Host,
    Interface,
    Neighbor,
    PluginError,
    Temperature,
    Transceiver,
    Vlan,
    WirelessClient,
    log,
)

from omini_omada.client import Client, Unavailable

T = TypeVar("T")

# linkSpeed codes. The device list, the gateway and the switch "speeds" list
# document 0: auto, 1: 10M, 2: 100M, 3: 1000M, 4: 2.5G, 5: 10G, 6: 5G, 7: 25G,
# 8: 100G, 9: 40G; older port descriptions stop at "4: 10Gbps" (written before
# 2.5G ports existed). The longer list is used.
LINK_SPEED = {1: 10, 2: 100, 3: 1000, 4: 2500, 5: 10000, 6: 5000, 7: 25000, 8: 100000, 9: 40000}
# radioId: 0: 2.4 GHz, 1: 5 GHz-1, 2: 5 GHz-2, 3: 6 GHz.
BANDS = {0: "2.4ghz", 1: "5ghz", 2: "5ghz", 3: "6ghz"}
BAND_NAMES = {"2.4ghz": "2.4 GHz", "5ghz": "5 GHz", "6ghz": "6 GHz"}
# Radio traffic blocks of GET .../aps/{apMac}/radios.
RADIOS = [
    ("radioTraffic2g", "2.4 GHz"),
    ("radioTraffic5g", "5 GHz"),
    ("radioTraffic5g2", "5 GHz-2"),
    ("radioTraffic6g", "6 GHz"),
]
ROLES = {"gateway": "router", "switch": "switch", "ap": "ap"}
# Switch port type: 1: copper, 2: combo (either), 3: SFP.
CONNECTORS = {1: "rj45", 3: "sfp"}
# Device status: 0 disconnected, 1 connected, 2 pending (not adopted),
# 3 heartbeat missed, 4 isolated. Disconnected and pending ones are left out.
SKIPPED_STATUS = (0, 2)
SOURCE = "omada"


# -- small helpers --------------------------------------------------------


def mac(value: Any) -> str | None:
    """aa:bb:cc:dd:ee:ff from any spelling (the API uses AA-BB-CC-DD-EE-FF)."""
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", value)
    if len(digits) != 12 or len(re.sub(r"[:\-. ]", "", value.strip())) != 12:
        return None
    digits = digits.lower()
    if digits == "0" * 12:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def api_mac(m: str) -> str:
    """The MAC as the API paths expect it: AA-BB-CC-DD-EE-FF."""
    return m.replace(":", "-").upper()


def num(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def counter(value: Any) -> int | None:
    n = num(value)
    return n if n is not None and n >= 0 else None


def pct(value: Any) -> float | None:
    n = num(value)
    return float(n) if n is not None and 0 <= n <= 100 else None


def text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def uptime_seconds(value: Any) -> int | None:
    """Uptime as the controller writes it: seconds, "3day(s) 4h 12m 9s",
    "3 days 04:12:09" or "04:12:09"."""
    n = num(value)
    if n is not None:
        return n if n >= 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.lower()
    total, found = 0, False
    days = re.search(r"(\d+)\s*d", s)
    if days:
        total += int(days.group(1)) * 86400
        found = True
    clock = re.search(r"(\d+):(\d{2}):(\d{2})", s)
    if clock:
        h, m, sec = (int(x) for x in clock.groups())
        return total + h * 3600 + m * 60 + sec
    for pattern, mult in ((r"(\d+)\s*h", 3600), (r"(\d+)\s*m(?!s)", 60), (r"(\d+)\s*s", 1)):
        hit = re.search(pattern, s)
        if hit:
            total += int(hit.group(1)) * mult
            found = True
    return total if found else None


def prefix_len(netmask: Any) -> int | None:
    if not isinstance(netmask, str):
        return None
    try:
        parts = [int(p) for p in netmask.split(".")]
    except ValueError:
        return None
    if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
        return None
    bits = "".join(f"{p:08b}" for p in parts)
    return bits.count("1") if "01" not in bits else None


def optional(what: str, fn: Callable[[], T]) -> T | None:
    """Data that some versions or roles do not offer: skipped on failure."""
    try:
        return fn()
    except Unavailable as e:
        log.debug("skipped %s: %s", what, e)
        return None


# -- devices --------------------------------------------------------------


@dataclass
class Built:
    kind: str
    fields: dict[str, Any]
    ports: dict[int, str] = field(default_factory=dict)  # port id -> interface name
    hosts: list[Host] = field(default_factory=list)
    wifi: list[WirelessClient] = field(default_factory=list)
    fdb: list[FdbEntry] = field(default_factory=list)

    def device(self) -> Device:
        f = dict(self.fields)
        if self.wifi:
            f["wireless_clients"] = self.wifi
        if self.fdb:
            f["fdb"] = self.fdb
        if self.hosts:
            f["hosts"] = self.hosts
        return Device(**f)


def base_fields(d: dict[str, Any], m: str, upgrades: dict[str, dict[str, Any]] | None) -> dict:
    ips = [ip for ip in [text(d.get("ip")), *(d.get("ipv6") or [])] if isinstance(ip, str) and ip]
    version = text(d.get("firmwareVersion"))
    firmware = None
    if version or upgrades is not None:
        latest, available = None, None
        if upgrades is not None:
            up = upgrades.get(m)
            if up is None:
                available = False
            else:
                need = up.get("needUpgrade")
                available = need if isinstance(need, bool) else True
                latest = text(up.get("latestVersion"))
        firmware = Firmware(current=version, latest=latest, update_available=available)
    return {
        "key": m,
        "name": text(d.get("name")) or m,
        "host": text(d.get("ip")),
        "role": ROLES.get(str(d.get("type") or "").lower(), "unknown"),
        "vendor": "TP-Link",
        "model": text(d.get("modelName")) or text(d.get("model")),
        "os_version": version,
        "serial": text(d.get("sn")),
        "uptime_s": uptime_seconds(d.get("uptime")),
        "cpu_pct": pct(d.get("cpuUtil")),
        "mem_pct": pct(d.get("memUtil")),
        "macs": [m],
        "ips": ips or None,
        "firmware": firmware,
    }


def uplink_neighbor(d: dict[str, Any], local_port: str | None) -> Neighbor | None:
    """The device the controller says this one hangs from (device list:
    uplinkDeviceMac / uplinkDevicePort)."""
    remote = mac(d.get("uplinkDeviceMac"))
    if not remote:
        return None
    return Neighbor(
        local_port=local_port or "Uplink",
        protocol="other",
        remote_mac=remote,
        remote_name=text(d.get("uplinkDeviceName")),
        remote_port=text(str(d.get("uplinkDevicePort") or "")),
    )


def build_switch(client: Client, site: str, d: dict, m: str, fields: dict) -> Built:
    b = Built("switch", fields)
    path = f"sites/{site}/switches/{api_mac(m)}"
    stat = optional("switch ports", lambda: client.get(f"sites/{site}/stat/switches/{api_mac(m)}"))
    stat = stat if isinstance(stat, dict) else {}
    interfaces: dict[int, Interface] = {}
    for p in stat.get("ports") or []:
        pid = num(p.get("port"))
        if pid is None:
            continue
        name = f"Port {pid}"
        label = text(p.get("name"))
        ps = p.get("portStatus") or {}
        up = None
        if ps.get("linkStatus") in (0, 1):
            up = ps.get("linkStatus") == 1
        if p.get("disable") is True:
            up = False
        interfaces[pid] = Interface(
            name=name,
            description=label if label and label.replace(" ", "").lower() != f"port{pid}" else None,
            type="ethernet",
            connector=CONNECTORS.get(num(p.get("type")) or 0),
            up=up,
            speed_mbps=LINK_SPEED.get(num(ps.get("linkSpeed")) or 0) if up else None,
            rx_bytes=counter(ps.get("rx")),
            tx_bytes=counter(ps.get("tx")),
        )
        b.ports[pid] = name

    # SFP diagnostics, only when the switch has fiber ports.
    if any(num(p.get("type")) in (2, 3) for p in stat.get("ports") or []):
        for ddm in optional("SFP diagnostics", lambda: client.get(f"{path}/ddm/info")) or []:
            pid = num(ddm.get("port")) if isinstance(ddm, dict) else None
            if pid not in interfaces or ddm.get("dataReady") == 0:
                continue
            interfaces[pid] = interfaces[pid].model_copy(
                update={
                    "transceiver": Transceiver(
                        temperature_c=ddm.get("temperature"),
                        voltage_v=ddm.get("voltage"),
                        bias_ma=ddm.get("biasCurrent"),
                        tx_power_dbm=ddm.get("txPowerDbm"),
                        rx_power_dbm=ddm.get("rxPowerDbm"),
                    )
                }
            )
    if interfaces:
        fields["interfaces"] = [interfaces[k] for k in sorted(interfaces)]

    neighbors: list[Neighbor] = []
    for n in optional("LLDP", lambda: client.pages(f"{path}/lldp-neighbors")) or []:
        pid = num(n.get("portId"))
        if pid is None:
            continue
        neighbors.append(
            Neighbor(
                local_port=b.ports.get(pid, f"Port {pid}"),
                protocol="lldp",
                remote_name=text(n.get("systemName")),
                remote_port=text(n.get("neighborPortId")),
                remote_mac=mac(n.get("deviceId")),
            )
        )
    uplink = stat.get("uplink") or {}
    up_port = num(uplink.get("port"))
    local = b.ports.get(up_port, f"Port {up_port}") if up_port is not None else None
    other = uplink_neighbor(d, local)
    if other and not any(
        x.remote_mac == other.remote_mac and x.local_port == other.local_port for x in neighbors
    ):
        neighbors.append(other)
    if neighbors:
        fields["neighbors"] = neighbors
    return b


def build_ap(client: Client, site: str, d: dict, m: str, fields: dict) -> Built:
    b = Built("ap", fields)
    path = f"sites/{site}/aps/{api_mac(m)}"
    info = optional("AP info", lambda: client.get(path))
    info = info if isinstance(info, dict) else {}
    if fields["uptime_s"] is None:
        fields["uptime_s"] = uptime_seconds(info.get("uptimeLong"))
    if fields["cpu_pct"] is None:
        fields["cpu_pct"] = pct(info.get("cpuUtil"))
    if fields["mem_pct"] is None:
        fields["mem_pct"] = pct(info.get("memoryUtil"))

    interfaces: list[Interface] = []
    uplink_port = None
    for p in optional("AP ports", lambda: client.get(f"{path}/ports")) or []:
        if not isinstance(p, dict):
            continue
        pid = num(p.get("port"))
        name = text(p.get("name")) or text(p.get("lanPort")) or (f"ETH{pid}" if pid else None)
        if not name:
            continue
        up = p.get("linkStatus") == 1 if p.get("linkStatus") in (0, 1) else None
        ps = p.get("portStatus") or {}
        duplex = {1: "half", 2: "full"}.get(num(p.get("duplex")) or 0)
        interfaces.append(
            Interface(
                name=name,
                type="ethernet",
                up=up,
                speed_mbps=LINK_SPEED.get(num(p.get("linkSpeed")) or 0) if up else None,
                duplex=duplex if up else None,
                rx_bytes=counter(ps.get("rx")),
                tx_bytes=counter(ps.get("tx")),
            )
        )
        if pid is not None:
            b.ports[pid] = name
        if p.get("uplinkPort") is True:
            uplink_port = name
    radios = optional("AP radios", lambda: client.get(f"{path}/radios"))
    for key, label in RADIOS:
        traffic = (radios or {}).get(key) if isinstance(radios, dict) else None
        if isinstance(traffic, dict):
            interfaces.append(
                Interface(
                    name=label,
                    type="wireless",
                    rx_bytes=counter(traffic.get("rx")),
                    tx_bytes=counter(traffic.get("tx")),
                )
            )
    if interfaces:
        fields["interfaces"] = interfaces

    # A mesh AP hangs from the AP it links to by Wi-Fi.
    wireless = info.get("wireless uplink info") or info.get("wirelessUplink") or {}
    if info.get("wirelessLinked") is True and mac(wireless.get("uplinkMac")):
        fields["neighbors"] = [
            Neighbor(
                local_port="Wi-Fi backhaul",
                protocol="other",
                remote_mac=mac(wireless.get("uplinkMac")),
                remote_name=text(wireless.get("name")),
            )
        ]
    else:
        n = uplink_neighbor(d, uplink_port)
        if n:
            fields["neighbors"] = [n]
    return b


def gateway_status(e: dict[str, Any]) -> str:
    if e.get("status") == 0:
        return "down"
    state = e.get("internetState")
    if state == 1:
        return "up"
    if state == 0:
        return "down"
    return "unknown"


def build_gateway(client: Client, site: str, d: dict, m: str, fields: dict) -> Built:
    b = Built("gateway", fields)
    path = f"sites/{site}/gateways/{api_mac(m)}"
    info = optional("gateway info", lambda: client.get(path))
    info = info if isinstance(info, dict) else {}
    if fields["uptime_s"] is None:
        fields["uptime_s"] = uptime_seconds(info.get("uptime"))
    if fields["cpu_pct"] is None:
        fields["cpu_pct"] = pct(info.get("cpuUtil"))
    if fields["mem_pct"] is None:
        fields["mem_pct"] = pct(info.get("memUtil"))
    temp = info.get("temp")
    if isinstance(temp, (int, float)) and not isinstance(temp, bool) and temp > 0:
        fields["temperatures"] = [Temperature(sensor="System", kind="board", celsius=temp)]

    interfaces: list[Interface] = []
    gateways: list[Gateway] = []
    for e in optional("WAN status", lambda: client.get(f"{path}/wan-status")) or []:
        if not isinstance(e, dict):
            continue
        pid = num(e.get("port"))
        name = text(e.get("name")) or (f"Port {pid}" if pid is not None else None)
        if not name:
            continue
        mode = num(e.get("mode"))
        wan = mode == 0 if mode is not None else num(e.get("type")) == 0
        up = e.get("status") == 1 if e.get("status") in (0, 1) else None
        v4 = e.get("wanPortIpv4Config") or {}
        ip = text(v4.get("ip")) or text(e.get("ip"))
        plen = prefix_len(v4.get("netmask"))
        duplex = {1: "half", 2: "full"}.get(num(e.get("duplex")) or 0)
        interfaces.append(
            Interface(
                name=name,
                description=text(e.get("portDesc")),
                type="ethernet",
                mac=mac(e.get("mac")),
                ips=[f"{ip}/{plen}" if plen is not None else ip] if ip else None,
                wan=True if wan else None,
                up=up,
                speed_mbps=LINK_SPEED.get(num(e.get("speed")) or 0) if up else None,
                duplex=duplex if up else None,
                rx_bytes=counter(e.get("rx")),
                tx_bytes=counter(e.get("tx")),
                rx_errors=counter(e.get("rxErrorPkts")),
                tx_errors=counter(e.get("txErrorPkts")),
            )
        )
        if pid is not None:
            b.ports[pid] = name
        if wan:
            loss = e.get("loss")
            latency = num(e.get("latency"))
            gateways.append(
                Gateway(
                    name=name,
                    interface=name,
                    address=text(v4.get("gateway")),
                    status=gateway_status(e),
                    rtt_ms=latency if latency is not None and latency >= 0 else None,
                    loss_pct=loss
                    if isinstance(loss, (int, float))
                    and 0 <= loss <= 100
                    and not isinstance(loss, bool)
                    else None,
                )
            )
    if interfaces:
        fields["interfaces"] = interfaces
    if gateways:
        fields["gateways"] = gateways

    vlans: list[Vlan] = []
    for lan in optional("LAN status", lambda: client.get(f"{path}/lan-status")) or []:
        vid = num(lan.get("vlan")) if isinstance(lan, dict) else None
        if vid is not None and 1 <= vid <= 4094:
            vlans.append(Vlan(id=vid, name=text(lan.get("lanName"))))
            ip = text(lan.get("ip"))
            if ip and ip not in (fields["ips"] or []):
                fields["ips"] = [*(fields["ips"] or []), ip]
    if vlans:
        fields["vlans"] = vlans
    return b


BUILDERS = {"gateway": build_gateway, "switch": build_switch, "ap": build_ap}


# -- clients --------------------------------------------------------------


def add_clients(built: dict[str, Built], clients: list[dict[str, Any]]) -> None:
    fallback = next((b for b in built.values() if b.kind == "gateway"), None) or next(
        iter(built.values()), None
    )
    for c in clients:
        cm = mac(c.get("mac"))
        if not cm or c.get("active") is False:
            continue
        kind = str(c.get("connectDevType") or "").lower()
        wireless = c.get("wireless") is True or (kind == "ap" and c.get("ssid") is not None)
        owner: Built | None = None
        if wireless:
            owner = built.get(mac(c.get("apMac")) or "")
            if owner:
                band = BANDS.get(num(c.get("radioId")) if c.get("radioId") is not None else -1)
                ssid = text(c.get("ssid"))
                label = " · ".join(x for x in (ssid, BAND_NAMES.get(band or "")) if x)
                rssi = num(c.get("rssi"))
                rx_kbps, tx_kbps = num(c.get("rxRate")), num(c.get("txRate"))
                down, upload = counter(c.get("activity")), counter(c.get("uploadActivity"))
                owner.wifi.append(
                    WirelessClient(
                        mac=cm,
                        interface=label or None,
                        ssid=ssid,
                        band=band,
                        signal_dbm=rssi if rssi is not None and rssi <= 0 else None,
                        # Negotiated rates in Kbit/s: tx = AP to client, rx = client to AP.
                        tx_rate_mbps=tx_kbps / 1000 if tx_kbps and tx_kbps > 0 else None,
                        rx_rate_mbps=rx_kbps / 1000 if rx_kbps and rx_kbps > 0 else None,
                        # Real-time rates in bytes/s.
                        rx_bps=down * 8 if down is not None else None,
                        tx_bps=upload * 8 if upload is not None else None,
                    )
                )
        else:
            dev = {"switch": "switchMac", "gateway": "gatewayMac", "ap": "apMac"}.get(kind)
            owner = built.get(mac(c.get(dev)) or "") if dev else None
            if owner:
                pid = num(c.get("port"))
                if pid is None:
                    lag = c.get("switchPortsInLag") or []
                    pid = num(lag[0]) if lag else None
                port = owner.ports.get(pid) if pid is not None else None
                if port is None and pid is not None and owner.kind == "switch":
                    port = f"Port {pid}"
                port = port or text(c.get("portName"))
                if port:
                    vid = num(c.get("vid"))
                    owner.fdb.append(
                        FdbEntry(mac=cm, port=port, vlan=vid if vid and 0 < vid < 4095 else None)
                    )
        ip = text(c.get("ip"))
        if ip:
            names = []
            for n in (text(c.get("name")), text(c.get("hostName"))):
                if n and n not in names and mac(n) != cm:
                    names.append(n)
            host = Host(
                ip=ip,
                mac=cm,
                hostnames=names or None,
                vendor=text(c.get("vendor")),
                model=text(c.get("model")),
                os=text(c.get("osName")),
                sources=[SOURCE],
            )
            target = owner or fallback
            if target:
                target.hosts.append(host)


def resolve_remote_ports(built: dict[str, Built]) -> None:
    """The device list names an uplink port by number ("5"): the plugin's own
    name for that port when the uplink device is in the site."""
    for b in built.values():
        neighbors = b.fields.get("neighbors") or []
        for i, n in enumerate(neighbors):
            if n.protocol != "other" or not n.remote_port or not n.remote_port.isdigit():
                continue
            remote = built.get(n.remote_mac or "")
            if remote is None:
                continue
            pid = int(n.remote_port)
            name = remote.ports.get(pid) or (f"Port {pid}" if remote.kind == "switch" else None)
            if name:
                neighbors[i] = n.model_copy(update={"remote_port": name})


# -- entry points ---------------------------------------------------------


def make_client(cfg: Config) -> Client:
    url = cfg.str("url")
    client_id = cfg.str("client_id")
    secret = cfg.str("client_secret")
    if not url or not client_id or not secret:
        raise PluginError("the controller address, client ID and client secret are required")
    return Client(
        url, client_id, secret, verify_tls=cfg.bool("verify_tls"), state_dir=cfg.state_dir
    )


def pick_site(client: Client, wanted: str) -> dict[str, Any]:
    try:
        sites = client.pages("sites")
    except Unavailable as e:
        raise PluginError(f"cannot list the sites: {e}") from e
    sites = [s for s in sites if s.get("siteId")]
    if not sites:
        raise PluginError(
            "the Open API application sees no site: give it access to the site "
            "(Site Privileges) in the controller"
        )
    if wanted:
        for s in sites:
            if str(s.get("name", "")).strip().lower() == wanted.lower() or s["siteId"] == wanted:
                return s
        names = ", ".join(str(s.get("name")) for s in sites)
        raise PluginError(f'no site named "{wanted}" (sites: {names})')
    return next((s for s in sites if s.get("primary") is True), sites[0])


def site_devices(client: Client, site: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return client.pages(f"sites/{site['siteId']}/devices")
    except Unavailable as e:
        raise PluginError(
            f'cannot read the devices of site "{site.get("name")}" ({e}): check the '
            "application's role and site privileges"
        ) from e


def collect(cfg: Config) -> list[Device]:
    client = make_client(cfg)
    try:
        site = pick_site(client, cfg.str("site"))
        sid = site["siteId"]
        rows = site_devices(client, site)
        upgrade_rows = optional(
            "upgradeable devices", lambda: client.pages(f"sites/{sid}/grid/devices/upgradeable")
        )
        upgrades = None
        if upgrade_rows is not None:
            upgrades = {m: r for r in upgrade_rows if (m := mac(r.get("mac")))}

        built: dict[str, Built] = {}
        for d in rows:
            m = mac(d.get("mac"))
            kind = str(d.get("type") or "").lower()
            if not m or num(d.get("status")) in SKIPPED_STATUS:
                continue
            fields = base_fields(d, m, upgrades)
            builder = BUILDERS.get(kind)
            built[m] = builder(client, sid, d, m, fields) if builder else Built(kind, fields)

        clients = optional("clients", lambda: client.pages(f"sites/{sid}/clients")) or []
        add_clients(built, clients)
        resolve_remote_ports(built)
        order = {"gateway": 0, "switch": 1, "ap": 2}
        return [b.device() for b in sorted(built.values(), key=lambda b: order.get(b.kind, 3))]
    finally:
        client.close()


def test(cfg: Config) -> str:
    client = make_client(cfg)
    try:
        version = text(str(client.info().get("controllerVer") or ""))
        client.token()
        site = pick_site(client, cfg.str("site"))
        rows = site_devices(client, site)
        kinds = [str(d.get("type") or "").lower() for d in rows]
        parts = [
            f"{n} {one if n == 1 else many}"
            for k, one, many in (
                ("gateway", "gateway", "gateways"),
                ("switch", "switch", "switches"),
                ("ap", "access point", "access points"),
            )
            if (n := kinds.count(k))
        ]
        found = ", ".join(parts) if parts else "no devices"
        ctrl = f"Omada Controller {version}" if version else "the Omada Controller"
        return f'Connected to {ctrl}, site "{site.get("name")}": {found}'
    finally:
        client.close()
