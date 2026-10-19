import json
from functools import partial

import httpx
import pytest
from omini_sdk import PluginError

import omini_omada.collect as collect_module
import omini_omada.radios as radios_module
from omini_omada.client import Client
from omini_omada.collect import collect, make_client
from omini_omada.radios import radios
from tests.conftest import OMADAC_ID, SITE

LIVING = "50:c7:bf:00:00:03"
GARDEN = "50:c7:bf:00:00:04"
AP_PATH = f"/openapi/v1/{OMADAC_ID}/sites/{SITE}/aps/50-C7-BF-00-00-03"


def by_key(devices):
    return {d.key: d for d in devices}


def test_reads_each_radio_of_an_access_point(omada, cfg):
    ap = by_key(collect(cfg))[LIVING]
    assert [r.model_dump(exclude_none=True) for r in ap.radios] == [
        {
            "name": "2.4 GHz",
            "band": "2.4ghz",
            "channel": 6,
            "width_mhz": 20,
            "auto": False,
            "tx_power_dbm": 20.0,
            "enabled": True,
        },
        # Channel 0 in the settings: the controller picks it; it is on 36 now.
        {
            "name": "5 GHz",
            "band": "5ghz",
            "channel": 36,
            "width_mhz": 80,
            "auto": True,
            "tx_power_dbm": 23.0,
            "enabled": True,
        },
    ]
    assert ap.wireless_uplink is False


def test_a_mesh_access_point_without_live_readings(omada, cfg):
    ap = by_key(collect(cfg))[GARDEN]
    assert ap.wireless_uplink is True
    # Only its settings: the configured channel, no width or power.
    assert [r.model_dump(exclude_none=True) for r in ap.radios] == [
        {"name": "2.4 GHz", "band": "2.4ghz", "auto": True, "enabled": False},
        {"name": "5 GHz", "band": "5ghz", "channel": 36, "auto": False, "enabled": True},
    ]


def test_other_devices_have_no_radios(omada, cfg):
    devices = by_key(collect(cfg))
    assert devices["50:c7:bf:00:00:02"].radios is None
    assert devices["50:c7:bf:00:00:02"].wireless_uplink is None


def test_unknown_widths_stay_empty():
    live = {"wp6g": {"actualChannel": "37", "bandWidth": "20/40MHz", "txPower": "x"}}
    assert radios({}, live) == [{"name": "6 GHz", "band": "6ghz", "channel": 37}]


# --- the channel change (not declared in plugin.yaml until verified) ------------


@pytest.fixture
def radio_api(omada, monkeypatch):
    """The controller, also taking PATCH .../aps/{apMac}/radio-config."""
    omada.patches = []
    omada.radio_stuck = False  # keeps the setting, the radio never moves
    inner = omada.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path == f"{AP_PATH}/radio-config":
            body = json.loads(request.content)
            omada.patches.append(body)
            info = omada.routes[AP_PATH]["result"]
            live = omada.routes[f"{AP_PATH}/radios"]["result"]
            for key, setting in body.items():
                info[key] = setting
                if not omada.radio_stuck and key == "radioSetting2g":
                    live["wp2g"]["actualChannel"] = str(setting["channel"])
            return httpx.Response(200, json={"errorCode": 0, "msg": "Success."})
        return inner(request)

    monkeypatch.setattr(
        collect_module,
        "Client",
        partial(Client, transport=httpx.MockTransport(handler), min_interval=0),
    )
    return omada


def test_moves_a_radio_keeping_its_other_settings(radio_api, cfg):
    c = make_client(cfg)
    radios_module.set_channel_unverified(
        c, SITE, "50-C7-BF-00-00-03", {"radio": "2.4 GHz", "channel": 11}
    )
    assert radio_api.patches == [
        {
            "radioSetting2g": {
                "radioEnable": True,
                "channelWidth": "2",
                "channel": "11",
                "txPower": 20,
                "txPowerLevel": 3,
            }
        }
    ]


def test_undone_when_the_radio_does_not_move(radio_api, cfg, monkeypatch):
    radio_api.radio_stuck = True
    with pytest.raises(PluginError, match=r"did not report channel 11 within 60 s \(undone\)$"):
        radios_module.set_channel_unverified(
            make_client(cfg), SITE, "50-C7-BF-00-00-03", {"radio": "2.4 GHz", "channel": 11}
        )
    assert radio_api.patches[-1]["radioSetting2g"]["channel"] == "6"


@pytest.mark.parametrize(
    ("ap", "value", "message"),
    [
        ("50-C7-BF-00-00-04", {"radio": "5 GHz", "channel": 40}, "mesh"),
        ("50-C7-BF-00-00-03", {"radio": "6 GHz", "channel": 37}, "no radio 6 GHz"),
        ("50-C7-BF-00-00-03", {"radio": "2.4 GHz", "channel": 36}, "not a channel"),
        ("50-C7-BF-00-00-03", {"radio": "2.4 GHz"}, "a number"),
    ],
)
def test_refused_channel_changes(radio_api, cfg, ap, value, message):
    with pytest.raises(PluginError, match=message):
        radios_module.set_channel_unverified(make_client(cfg), SITE, ap, value)
    assert radio_api.patches == []


def test_wifi_channel_is_not_declared():
    from pathlib import Path

    manifest = (Path(__file__).parent.parent / "plugin.yaml").read_text()
    assert "wifi.channel" not in manifest
