# omini-plugin-omada

[Omini](https://github.com/riccardoalv/omini) plugin for **TP-Link Omada**, through the controller's official **Omada Open API**. It reads every gateway, switch and access point of a site and, when you ask for it in Omini, enables or disables switch ports, sets their VLANs, creates, renames and deletes the site's VLANs and manages each VLAN's Wi-Fi. It changes nothing else: it never starts a scan, a firmware check or an upgrade.

> **Experimental.** This plugin was built from TP-Link's Open API documentation and tested against recorded answers shaped like the documented ones (`tests/fixtures`), **not on real hardware**. Reports from Omada users are welcome.

## What it reads

One Omini device per managed gateway, switch and access point (its key is its MAC). Disconnected and not yet adopted devices are left out.

| Data | Used for |
|---|---|
| Devices of the site: name, model, IP, firmware, serial, uptime, CPU, memory | Devices on the map and their health |
| Upgradeable devices (the controller's own list) | Pending firmware updates and the latest version |
| Switch ports: status, link speed, RJ45/SFP, traffic counters, the switch's uplink port | Ports of the switch, link speeds, traffic |
| Switch SFP diagnostics (DDM): temperature, voltage, bias, TX/RX power | Transceiver health |
| Switch ports' spanning tree state (`portStatus.stpDiscarding`: discarding is blocking; otherwise forwarding while the link is up, disabled while it is down) | Standby links (a blocking port) on the map |
| Switch ports' LAG (`lagId` in the switch statistics): each member gets `LAG <id>`, and the LAG itself is reported (type `lag`, its member ports, up when a member is, its speed the sum of the members up) | The cables of a LAG drawn as one link |
| Switch LLDP neighbors | Links between devices |
| Uplink of each device (device list) and the Wi-Fi uplink of mesh APs | Where each device hangs on the map |
| Gateway WANs and ports: status, speed, IP, traffic counters, internet state, latency, loss | WAN nodes, gateway status, ports |
| Gateway LAN networks (VLAN id and name), temperature | VLANs and gateway health |
| The site's LAN networks (VLAN id and name), the port profiles and each switch port's profile | The VLANs of every switch and of each switch port (native and tagged) |
| AP Ethernet ports and per-radio traffic counters (2.4 / 5 / 6 GHz) | Ports and traffic of the AP |
| Online clients: Wi-Fi ones with SSID, band, signal (dBm), link rates and current traffic; wired ones with switch / gateway / AP port and VLAN; names, vendor, OS, IP | Clients on the map under the right AP or port, and their names |

## What it changes

**Enable or disable a switch port** (`port.enabled`), from the port's card in Omini. Only ports of Omada switches; ports of gateways and access points are refused with a message.

1. The plugin reads the switch's ports (`GET .../sites/{siteId}/stat/switches/{switchMac}`) and finds the port by its number (Omini's `Port 5` is port `5`). If the port is already in the requested state, nothing is sent.
2. It sends `PUT /openapi/v1/{omadacId}/sites/{siteId}/switches/{switchMac}/ports/{port}/status` with `{"status": 0}` (off) or `{"status": 1}` (on): Switch → *Set port status for given port*. Only the port's on/off state travels; its profile, name, VLANs, PoE and the rest of its settings are not part of the request and stay as they are.
3. It reads the switch's ports again (for about 40 seconds, every 3 seconds) until the port's `disable` flag shows the change; if it does not, it sends the previous status back and the change is reported as failed.
4. There is nothing to save: the controller keeps the change in its own configuration and pushes it to the switch (also after the switch reboots or is re-adopted).

The change needs an application whose role may modify the site's devices (see below). With a **Viewer** application the map works as before, and a change fails with a message that says which role to give the application.

Collecting only reads: no other endpoint that changes configuration, starts a check or an upgrade is ever called.

### Wi-Fi per VLAN

`wifi.set` and `wifi.delete` manage the site's Wi-Fi networks through the Open API's wireless network endpoints, from the VLANs tab of Omini: the SSID's name, its VLAN (`vlanSetting`), its password and whether it is on (`.../ssids/{id}/enable`). A new SSID is WPA2 personal (`security` 3, `versionPsk` 2) on both bands, in the site's first WLAN group. The controller keeps it and pushes it to every access point; the plugin reads the site again until it has it. The password goes to the controller only. An enterprise (802.1X) SSID shows no security in Omini. Omini shows one Wi-Fi network per name, so a name used by SSIDs of several WLAN groups cannot be changed or deleted from it (the change is refused with a message). Not verified on hardware: the request bodies follow the controller's web API, which the Open API mirrors.

### VLANs

Omada keeps VLANs per **site**: they are the site's LAN networks (*Settings → Wired Networks → LAN*), and every switch of the site carries all of them. So each Omada switch reports the site's networks as its VLANs, and a switch port's VLANs are those of its **port profile**: the profile's native network is the port's untagged VLAN, its tagged networks the tagged ones.

- **`vlan.set`** (`{id, name}`) creates a **VLAN-only** network (purpose *VLAN*, no subnet or DHCP: routing stays on your router) with that VLAN id, or renames the network that has it (the network is sent back as read, with only the name changed). Creating a network makes the controller add its own profile for it (native = that network).
- **`vlan.delete`** (`{id}`) deletes a VLAN-only network. A gateway network (purpose *Interface*, with its own subnet and DHCP) is refused with a message: delete it in the controller if you mean to. The controller itself refuses to delete a network that is the native network of a profile in use; the message says to move those ports first.
- **`port.vlans`** (`{untagged, tagged}`) gives the switch port a profile with exactly those VLANs (Switch → *Set profile for given port*). The plugin never modifies a profile (other ports use them): it picks an existing profile with exactly those VLANs **and** the same other settings as the port's current profile (PoE, 802.1X, port isolation, LLDP-MED, bandwidth/storm control, spanning tree, loopback detection, EEE, flow control, DHCP L2 relay), so the port keeps everything but its VLANs; if there is none, it creates one copied from the current profile with the VLANs replaced, named `Omini <native> <tagged,…>` (for example `Omini 10 20,30`; `(2)`, `(3)`… when the name is taken). Then it reads the switch again until the port shows the new profile and VLANs; if it does not, the port goes back to its previous profile and the profile made for it is deleted.
  - Every Omada port has a native network: a trunk without an untagged VLAN is refused.
  - Ports in a **LAG**, ports with **Profile Override** on (their own settings win over the profile) and ports whose profile has extra untagged networks or is *Disable* show no VLANs in Omini and cannot be changed from it (a change is refused with the reason).
  - Profiles made by Omini stay in the controller when no port uses them any more; delete them there if you like.

There is no global "VLAN mode" to turn on in Omada: a switch adopted by the controller always follows the site's networks and profiles. All of this goes through the controller, which keeps the change and pushes it to the switches; there is nothing to save on the switch.

## Configuration backup

Omini keeps a history of each device's configuration (never restored from it). The Open API has no text export, so the plugin's backup is **JSON with sorted keys**, one per device of the site that collection reports (keyed by its MAC):

- **Each device:** its name, model and type (device list) and the settings in *Get switch info* / *Get gateway info* / *Get AP info*: a switch's **port list** (each port's name, profile, profile override, PoE mode, LAG, on/off), a gateway's **port settings** (`portConfigs`), and the IP, LED, radio and management VLAN settings when the controller lists them there. Status (uptime, CPU, memory, temperature, firmware, link state, counters, clients) is left out.
- **The site's settings**, in one device's backup only, so that a change to the site is one change: the gateway's, else the switch with the lowest MAC (else the access point with the lowest MAC). They are the LAN networks (`lan-networks`), the port profiles (`lan-profiles`) and the Wi-Fi networks (the SSIDs of every WLAN group).

**Secrets are left out**: the SSIDs' passwords (`pskSetting.securityKey`) and any field named like a password, passphrase, secret, security or private key, or `pwd` (RADIUS, PPPoE, VPN), at any depth. Every list is sorted (ports by number, networks, profiles and SSIDs by name), so two backups of an unchanged site are equal and differ only when the configuration changed. An endpoint the application's role or the controller's version lacks is left out the same way every time. A site setting (networks, profiles, Wi-Fi) the controller fails to answer is left out of that backup (and logged) while the devices' settings are still backed up; an error reading a device's own settings fails the backup rather than produce a partial one. The backup only reads (the GET requests above; a **Viewer** application can make it). The content is never logged.

## Requirements

- **Omada Software Controller** or a hardware controller **OC200 / OC300**, **version 5.9 or later** (the Open API appeared in 5.9; client mode with *Site Privileges* in later 5.x releases).
- The **Omada Cloud-Based Controller** is out of scope (its Open API lives on a regional TP-Link domain, not on your network).
- The controller must be reachable from Omini over HTTPS (Software Controller: port 8043 by default; OC200/OC300: 443).

## Create the Open API application

The plugin uses an Open API application in **client mode** (client credentials). To change things from Omini it needs the **Administrator** role, or a custom role with **Modify** permission on **devices** (*Site Device Manager*: port status and port profile) and on **site settings** (*Site Settings Manager*: LAN networks, port profiles, Wi-Fi); a **Viewer** application is enough for the map alone (changes then fail with a message that says which role to give it).

1. Log in to the controller as an administrator and switch to the **Global** view.
2. Go to **Settings → Platform Integration → Open API** and click **Add New App**.
3. Fill in:
   - **App Name**: `Omini`
   - **Mode**: **Client**
   - **Role**: **Administrator** (or a custom role with **Modify** permission on devices and site settings). Choose **Viewer** instead if Omini should only read: it can only view status and settings.
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

All endpoints are from the official Open API reference, [use1-omada-northbound.tplinkcloud.com/doc.html](https://use1-omada-northbound.tplinkcloud.com/doc.html) (the same reference is under *Online API Document* in the controller's Open API page), and TP-Link's support articles [How to Create Site in Omada Controller via Open API](https://support.omadanetworks.com/en/document/109315/) (creating an Open API app in client mode, getting a token) and [How to configure Account on Omada Controller](https://support.omadanetworks.com/en/document/13313/) (the Administrator and Viewer roles). The "Open API Access Guide" (token, error codes, rate limit) is the home page of the reference. Every answer is `{"errorCode": 0, "msg": "...", "result": ...}`; any other `errorCode` is a failure.

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
| Backup: the devices' settings (backup only) | `GET .../sites/{siteId}/switches/{switchMac}`, `.../gateways/{gatewayMac}`, `.../aps/{apMac}`; the site's `lan-networks`, `lan-profiles` and `wireless-network/wlans/{wlanId}/ssids` | Switch → *Get switch info*, Gateway → *Get gateway info*, Ap → *Get AP info*, Site, Wireless network |
| Enable / disable a switch port (only when asked in Omini) | `PUT .../sites/{siteId}/switches/{switchMac}/ports/{port}/status`, body `{"status": 0 \| 1}` | Switch → *Set port status for given port* |
| The site's networks (VLANs) | `GET /openapi/v2/{omadacId}/sites/{siteId}/lan-networks` (paged); `GET /openapi/v1/...` on controllers without v2 | Site → *Get LAN network list* (v2, and the deprecated v1) |
| Port profiles | `GET .../sites/{siteId}/lan-profiles` (paged) | Site → *Get LAN profile list* |
| Each switch port's profile | `GET .../sites/{siteId}/switches/{switchMac}` → `portList[]`: `port`, `profileId`, `profileOverrideEnable`, `lagPort` | Switch → *Get switch info* |
| Create a VLAN (only when asked) | `POST /openapi/v2/.../sites/{siteId}/lan-networks` (v1 when v2 is missing), body `{name, purpose: 0, vlan, igmpSnoopEnable: false, mldSnoopEnable: false}` | Site → *Create LAN network* |
| Rename a VLAN (only when asked) | `PATCH /openapi/v2/.../sites/{siteId}/lan-networks/{networkId}` (v1 when v2 is missing), body: the network as read with the new name | Site → *Modify LAN network* |
| Delete a VLAN (only when asked) | `DELETE .../sites/{siteId}/lan-networks/{networkId}` | Site → *Delete LAN network* |
| A profile for a port's VLANs (only when asked) | `POST .../sites/{siteId}/lan-profiles`, body: the port's current profile settings with `name`, `nativeNetworkId`, `tagNetworkIds`, `untagNetworkIds: []`; `DELETE .../lan-profiles/{profileId}` only to undo a failed change | Site → *Create new LAN profile*, *Delete an existing LAN profile* |
| Set a port's VLANs (only when asked) | `PUT .../sites/{siteId}/switches/{switchMac}/ports/{port}/profile`, body `{"profileId": ...}` | Switch → *Set profile for given port* |

Notes:

- **Collecting only reads.** The one `POST` it sends is the token request; the writes above (port status and profile, LAN networks, port profiles, Wi-Fi) are sent only to apply a change asked for in Omini. No endpoint that starts a check or an upgrade is ever called.
- **Token** kept in the integration's state folder (file readable only by Omini, tied to the address and credentials by a hash; the secret itself is not stored there). When it expires (error `-44112`/`-44113`) the plugin asks for a new one with the client credentials instead of the refresh token, whose request would carry the secret in the URL.
- **Rate limit**: the controller allows about 10 requests per second; the plugin spaces its requests (~8 per second) and waits a second on HTTP 429. A site with *N* switches and *M* APs takes about 8 + 4·N + 3·M + 3 per gateway requests.
- **Optional data** (anything but the site list and the device list) is skipped when an endpoint is missing on the controller's version or forbidden to the app's role: the rest of the collection goes on.
- Paged lists are read 1000 rows at a time until `totalRows`.
- Switch ports are named `Port 1`, `Port 2`… after their port number (stable even when renamed in the controller); the name given in the controller becomes the port's description.

## Known limitations (not verified without hardware)

- **Link speed codes.** The reference documents `linkSpeed` as `1: 10M, 2: 100M, 3: 1000M, 4: 2.5G, 5: 10G, 6: 5G, 7: 25G, 8: 100G, 9: 40G` for devices, gateways and the switch speed list, but as `4: 10Gbps` in some port descriptions written before 2.5G ports existed. The plugin uses the first list; a 10G port could show as 2.5G if a firmware uses the older code.
- **Uptime** of the device list is a string; the formats `3day(s) 4h 12m 9s`, `3 days 04:12:09` and plain seconds are understood (APs also report `uptimeLong` in seconds).
- **Gateway ports**: `wan-status` is used for every port it lists, WAN (`mode` 0) or LAN. If a controller lists only WANs there, the gateway's LAN ports do not appear (its LAN networks still do, as VLANs).
- **Stacked switches** (stack ports `unit/slot/port`) are read as plain ports.
- **STP and LAGs (not verified on hardware).** Assumed: `portStatus.stpDiscarding` is `true` on a port spanning tree blocks (and `false` when spanning tree is off, so a switch without it shows its ports forwarding); the port role and the root bridge are not in the Open API, so they are not reported; a LAG member carries its LAG's number as `lagId` in *Get switch statistics*; the controller does not list a LAG as a port of its own, so the plugin builds it from its members. SFP modules' temperature alarm thresholds are not in `ddm/info`, so Omini's default applies to hot modules.
- **Backup (not verified on hardware).** The settings fields kept from *Get switch info*, *Get gateway info* and *Get AP info* (`portList`, `portConfigs`, `ipSetting`, `ledSetting`, `radioSetting*`, `mvlanSetting`...) are taken when present; whichever of them a controller does not return is simply absent. The SSIDs' password field is assumed to be `pskSetting.securityKey`, as in the create and update bodies.
- **Gateway temperature** is reported as a board sensor; the API does not say where it is measured.
- Wired clients connected to a switch port behind a LAG use the first port of the LAG.
- The Omada **Cloud-Based Controller** and MSP mode are not supported.
- **Port changes** use *Set port status for given port*, which the current reference documents; older controllers that lack it answer with an error, and the change fails without touching anything. Ports in a LAG and stack ports are not handled specially.
- **VLANs (not verified on hardware).** Built from the Open API reference (the `lan-networks`, `lan-profiles`, *Get switch info* and *Set profile for given port* operations and their documented fields), not from a real controller. Assumed: the v2 `lan-networks` list and writes exist on 5.15+ and v1 (deprecated) answers on older 5.x; `purpose` 0 is a VLAN-only network and 1 a gateway interface, `vlanType` 1 a multi-VLAN interface (left out); a VLAN-only network is created with just `name`, `purpose`, `vlan` and the IGMP/MLD snooping flags; renaming sends the network back as read (minus `id`, portal/ACL/rate-limit status and the `exist…`/`support…` flags) with the new name; *Get switch info* lists every port with its `profileId`, `profileOverrideEnable` and `lagPort`; the built-in *All* profile (`type` 0) with no tagged list carries every other network tagged; a profile's `untagNetworkIds` other than its native network make the port's VLANs unrepresentable; *Create new LAN profile* answers the new profile's `id` (else it is found by name); and the profile change shows in *Get switch info* within a few seconds. Easy Managed switches (which the controller answers through other paths) and stacked switches are not handled.

## Install

Omada is in Omini's plugin catalog: **Integrations → Add → TP-Link Omada** installs it and opens its form (or **Integrations → Plugin store → Available**). It can also be installed from its address, `https://github.com/riccardoalv/omini-plugin-omada`.

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
