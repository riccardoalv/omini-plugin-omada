import httpx
import pytest
from conftest import OMADAC_ID, SITE
from omini_sdk import Change, PluginError

import omini_omada.collect as collect_module
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
    reads = []
    inner = omada.handler

    def handler(request):
        if request.method == "GET" and request.url.path == STAT:
            reads.append(1)
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    with pytest.raises(
        PluginError, match=r"did not apply the change to Port 6: .* within 40 s \(undone\)$"
    ):
        apply(cfg, change())
    # Sent once, read again for the whole window, never retried blindly; then
    # the previous status is sent back so nothing is left half-changed.
    assert omada.puts == [(STATUS.format(6), {"status": 0}), (STATUS.format(6), {"status": 1})]
    assert len(reads) == 1 + collect_module.VERIFY_S // collect_module.POLL_S + 1


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


def test_the_port_change_is_awaited_like_the_other_changes():
    # The controller pushes the change to the switch: read it again for
    # about 40 s (as the UniFi plugin does), not 3.
    assert collect_module.VERIFY_S >= 30
