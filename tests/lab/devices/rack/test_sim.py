"""Tests for RackSim camelCase state properties."""

from wetlab.lab.sim import SimBackend


def test_rack_sim_camelcase_properties_and_read():
    backend = SimBackend()
    st = backend.state()["T006001"]
    assert st["rackState"] == 0
    assert st["rackStateText"] == "正常"
    assert st["errorCode"] == 0
    assert st["errorText"] == ""

    res = backend.call("T006001", "GetStatus", {}, 15)
    assert res["rackState"] == 0
    assert res["errorCode"] == 0
