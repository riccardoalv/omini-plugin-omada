# omini-plugin-omada

[Omini](https://github.com/riccardoalv/omini) plugin for **TP-Link Omada**, through the controller's official **Omada Open API**. It reads every gateway, switch and access point of a site. Read-only: it never changes the controller's or the devices' configuration, and never starts a scan, a firmware check or an upgrade.

> **Experimental.** This plugin was built from TP-Link's Open API documentation and tested against recorded answers shaped like the documented ones (`tests/fixtures`), **not on real hardware**. Reports from Omada users are welcome.

## What it reads

One Omini device per managed gateway, switch and access point (its key is its MAC). Disconnected and not yet adopted devices are left out.

| Data | Used for |
|---|---|
| Devices of the site: name, model, IP, firmware, serial, uptime, CPU, memory | Devices on the map and their health |
| Upgradeable devices (the controller's own list) | Pending firmware updates and the latest version |
| Switch ports: status, link speed, RJ45/SFP, traffic counters, the switch's uplink port | Ports of the switch, link speeds, traffic |
| Switch SFP diagnostics (DDM): temperature, voltage, bias, TX/RX power | Transceiver health |
| Switch LLDP neighbors | Links between devices |
| Uplink of each device (device list) and the Wi-Fi uplink of mesh APs | Where each device hangs on the map |
| Gateway WANs and ports: status, speed, IP, traffic counters, internet state, latency, loss | WAN nodes, gateway status, ports |
| Gateway LAN networks (VLAN id and name), temperature | VLANs and gateway health |
| AP Ethernet ports and per-radio traffic counters (2.4 / 5 / 6 GHz) | Ports and traffic of the AP |
| Online clients: Wi-Fi ones with SSID, band, signal (dBm), link rates and current traffic; wired ones with switch / gateway / AP port and VLAN; names, vendor, OS, IP | Clients on the map under the right AP or port, and their names |

## Requirements

- **Omada Software Controller** or a hardware controller **OC200 / OC300**, **version 5.9 or later** (the Open API appeared in 5.9; client mode with *Site Privileges* in later 5.x releases).
- The **Omada Cloud-Based Controller** is out of scope (its Open API lives on a regional TP-Link domain, not on your network).
- The controller must be reachable from Omini over HTTPS (Software Controller: port 8043 by default; OC200/OC300: 443).

## Create the Open API application (least privilege)

The plugin uses an Open API application in **client mode** (client credentials) with the **Viewer** role, which can only read:

1. Log in to the controller as an administrator and switch to the **Global** view.
2. Go to **Settings → Platform Integration → Open API** and click **Add New App**.
3. Fill in:
   - **App Name**: `Omini`
   - **Mode**: **Client**
   - **Role**: **Viewer** (it can only view status and settings; do not use an administrator role)
   - **Site Privileges**: only the site(s) Omini should map.
4. Click **Apply**. The app appears in the list: reveal and copy its **Client ID** and **Client Secret** (eye icon), and check the **Interface Access Address** (view icon) — it is the address to give Omini.
5. In Omini: **Integrations → Add → TP-Link Omada**, fill in the fields below and click **Test connection**.

Changing the app's role or site privileges, or copying/importing sites, invalidates its tokens; the plugin then simply asks for a new one.

## Form fields

| Field | Meaning |
|---|---|
| Controller address | The controller's address (the app's *Interface Access Address*), e.g. `https://192.168.1.10:8043` (Software Controller) or `https://192.168.1.2` (OC200/OC300) |
| Client ID | The app's client ID |
| Client secret | The app's client secret (stored encrypted by Omini, never logged) |
| Site | Name of the site to read. Empty: the controller's default site (else the first one the app can see). One integration reads one site; add another integration for another site |
| Verify the TLS certificate | Off by default: controllers use a self-signed certificate. Turn it on if yours is trusted |

## How it works

All endpoints are from the official Open API reference, [use1-omada-northbound.tplinkcloud.com/doc.html](https://use1-omada-northbound.tplinkcloud.com/doc.html) (the same reference is under *Online API Document* in the controller's Open API page), and TP-Link's support articles [How to Create Site in Omada Controller via Open API](https://support.omadanetworks.com/en/document/109315/) (creating an Open API app in client mode, getting a token) and [How to configure Account on Omada Controller](https://support.omadanetworks.com/en/document/13313/) (the Viewer role). The "Open API Access Guide" (token, error codes, rate limit) is the home page of the reference. Every answer is `{"errorCode": 0, "msg": "...", "result": ...}`; any other `errorCode` is a failure.

| Step | Request | Reference |
|---|---|---|
| Omada ID and controller version | `GET /api/info` → `result.omadacId`, `result.controllerVer` (no login needed) | The controller's own web interface reads it; not part of the Open API reference |
| Access token | `POST /openapi/authorize/token?grant_type=client_credentials`, body `{omadacId, client_id, client_secret}` → `result.accessToken`, `expiresIn` (7200 s) | Access Guide 2.3.1 |
| Authentication | Header `Authorization: AccessToken=<token>` | Access Guide 2.2.3 |
| Sites | `GET /openapi/v1/{omadacId}/sites` | Site → *Get site list* |
| Devices | `GET .../sites/{siteId}/devices` | Device → *Get site device list* |
| Pending updates | `GET .../sites/{siteId}/grid/devices/upgradeable` | Device → *Get upgradeable devices on site view* |
| Switch ports | `GET .../sites/{siteId}/stat/switches/{switchMac}` | Statistic → *Get switch statistics* |
| SFP diagnostics | `GET .../sites/{siteId}/switches/{switchMac}/ddm/info` (only when the switch has SFP/combo ports) | Switch → *Get osw ddm info* |
| LLDP | `GET .../sites/{siteId}/switches/{switchMac}/lldp-neighbors` | Switch → *Get switch lldp neighbor table* |
| Gateway | `GET .../sites/{siteId}/gateways/{gatewayMac}` | Gateway → *Get gateway info* |
| WANs and ports | `GET .../sites/{siteId}/gateways/{gatewayMac}/wan-status` | Gateway → *Get gateway wan status* |
| LAN networks | `GET .../sites/{siteId}/gateways/{gatewayMac}/lan-status` | Gateway → *Get gateway lan status* |
| AP | `GET .../sites/{siteId}/aps/{apMac}` (uptime, mesh uplink) | Ap → *Get AP info* |
| AP ports | `GET .../sites/{siteId}/aps/{apMac}/ports` | Ap → *Get AP port list* |
| AP radios | `GET .../sites/{siteId}/aps/{apMac}/radios` | Ap → *Get AP radio detail* |
| Clients | `GET .../sites/{siteId}/clients` | Client → *Get client list* |

Notes:

- **Only reads.** The one `POST` is the token request. No endpoint that changes configuration, starts a check or an upgrade is ever called.
- **Token** kept in the integration's state folder (file readable only by Omini, tied to the address and credentials by a hash; the secret itself is not stored there). When it expires (error `-44112`/`-44113`) the plugin asks for a new one with the client credentials instead of the refresh token, whose request would carry the secret in the URL.
- **Rate limit**: the controller allows about 10 requests per second; the plugin spaces its requests (~8 per second) and waits a second on HTTP 429. A site with *N* switches and *M* APs takes about 6 + 3·N + 3·M + 3 per gateway requests.
- **Optional data** (anything but the site list and the device list) is skipped when an endpoint is missing on the controller's version or forbidden to the app's role: the rest of the collection goes on.
- Paged lists are read 1000 rows at a time until `totalRows`.
- Switch ports are named `Port 1`, `Port 2`… after their port number (stable even when renamed in the controller); the name given in the controller becomes the port's description.

## Known limitations (not verified without hardware)

- **Link speed codes.** The reference documents `linkSpeed` as `1: 10M, 2: 100M, 3: 1000M, 4: 2.5G, 5: 10G, 6: 5G, 7: 25G, 8: 100G, 9: 40G` for devices, gateways and the switch speed list, but as `4: 10Gbps` in some port descriptions written before 2.5G ports existed. The plugin uses the first list; a 10G port could show as 2.5G if a firmware uses the older code.
- **Uptime** of the device list is a string; the formats `3day(s) 4h 12m 9s`, `3 days 04:12:09` and plain seconds are understood (APs also report `uptimeLong` in seconds).
- **Gateway ports**: `wan-status` is used for every port it lists, WAN (`mode` 0) or LAN. If a controller lists only WANs there, the gateway's LAN ports do not appear (its LAN networks still do, as VLANs).
- **Stacked switches** (stack ports `unit/slot/port`) and switches in a LAG are read as plain ports; LAG membership is not reported.
- **Gateway temperature** is reported as a board sensor; the API does not say where it is measured.
- Wired clients connected to a switch port behind a LAG use the first port of the LAG.
- The Omada **Cloud-Based Controller** and MSP mode are not supported.

## Install

Omada is in Omini's plugin catalog: **Integrations → Add → TP-Link Omada** installs it and opens its form (or **Settings → Plugins → Available**). It can also be installed from its address, `https://github.com/riccardoalv/omini-plugin-omada`.

## Development

The plugin uses [uv](https://docs.astral.sh/uv/) and expects the Omini repository next to it (the SDK is in `../omini/sdk/python`):

```bash
uv run pytest          # tests, against answers shaped like the documented ones (tests/fixtures)
uv run ruff check .    # lint
uv run ruff format .   # format
```

Run it from a local Omini without installing it: `OMINI_PLUGIN_DIRS=../omini-plugin-omada make run` in the Omini repository; every collection uses the current code.

## License

MIT
