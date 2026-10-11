import pytest
from conftest import OMADAC_ID, SITE
from omini_sdk import Change, PluginError

from omini_omada.collect import apply, collect

SWITCH = "50:c7:bf:00:00:02"
STAT = f"/openapi/v1/{OMADAC_ID}/sites/{SITE}/stat/switches/50-C7-BF-00-00-02"
STATUS = f"sites/{SITE}/switches/50-C7-BF-00-00-02/ports/{{}}/status"


def change(port="Port 6", value=False, device=SWITCH):
    return Change(device=device, kind="port.enabled", port=port, value=value)


def ports(devices, key=SWITCH):
    sw = next(d for d in devices if d.key == key)
    return {i.name: i for i in sw.interfaces}


def stat_port(omada, n):
    return next(p for p in omada.routes[STAT]["result"]["ports"] if p["port"] == n)


def test_reads_whether_each_port_is_enabled(omada, cfg):
    stat_port(omada, 6)["disable"] = True
    del stat_port(omada, 25)["disable"]
    p = ports(collect(cfg))
    assert (p["Port 1"].enabled, p["Port 6"].enabled, p["Port 6"].up) == (True, False, False)
    assert p["Port 25"].enabled is None  # not reported: left empty


def test_disables_a_port_keeping_its_other_settings(omada, cfg):
    before = dict(stat_port(omada, 5))
    devices = apply(cfg, change("Port 5"))
    # Only the port's on/off state is sent: its profile, name and the rest of
    # its settings are not part of the request.
    assert omada.puts == [(STATUS.format(5), {"status": 0})]
    assert ports(devices)["Port 5"].enabled is False
    assert ports(devices)["Port 5"].description == "AP Living room"
    assert {k: v for k, v in stat_port(omada, 5).items() if k != "disable"} == {
        k: v for k, v in before.items() if k != "disable"
    }


def test_enables_a_disabled_port(omada, cfg):
    stat_port(omada, 6)["disable"] = True
    devices = apply(cfg, change(value=True))
    assert omada.puts == [(STATUS.format(6), {"status": 1})]
    assert ports(devices)["Port 6"].enabled is True


def test_an_error_when_the_controller_did_not_take_it(omada, cfg):
    omada.ignore_changes = True
    with pytest.raises(PluginError, match="did not apply the change to Port 6"):
        apply(cfg, change())
    assert len(omada.puts) == 1  # sent once, then read again; never retried blindly


def test_nothing_is_sent_when_the_port_is_already_so(omada, cfg):
    apply(cfg, change(value=True))
    assert omada.puts == []


def test_unknown_ports_devices_and_values(omada, cfg):
    with pytest.raises(PluginError, match="has no Port 7"):
        apply(cfg, change("Port 7"))
    with pytest.raises(PluginError, match="has no ETH1"):
        apply(cfg, change("ETH1"))
    with pytest.raises(PluginError, match="not in site"):
        apply(cfg, change(device="aa:bb:cc:dd:ee:ff"))
    with pytest.raises(PluginError, match="cannot apply"):
        apply(cfg, change(value="off"))
    assert omada.puts == []


def test_only_switch_ports(omada, cfg):
    with pytest.raises(PluginError, match="access point: only switch ports"):
        apply(cfg, change("ETH1", device="50:c7:bf:00:00:03"))
    with pytest.raises(PluginError, match="gateway: only switch ports"):
        apply(cfg, change("Port 1", device="50:c7:bf:00:00:01"))
    assert omada.puts == []


def test_a_viewer_application_is_told_which_role_it_needs(omada, cfg):
    # A Viewer app reads the site but its writes are "Operation forbidden".
    omada.forbidden.add(f"sites/{SITE}/switches/50-C7-BF-00-00-02/ports")
    with pytest.raises(PluginError, match="Administrator role") as e:
        apply(cfg, change())
    assert "Modify" in str(e.value) and "Site Privileges" in str(e.value)
    assert stat_port(omada, 6)["disable"] is False
