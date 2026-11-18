"""Capture-replay tests for `wetlab.lab.mqtt.codec` and device `spec.result_ok`."""

import json
from pathlib import Path

from wetlab.lab.devices import arm, gateway, pcr, rack, shaker, turntable
from wetlab.lab.mqtt.codec import (
    IgnoredFrame,
    RpcResult,
    UnrecognizedFrame,
    build_request,
    parse_uplink,
)

CAPTURES = Path(__file__).resolve().parents[3] / "captures"


def _capture_line(filename: str, lineno: int) -> dict:
    lines = (CAPTURES / filename).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[lineno - 1])


def test_build_request_matches_all_recorded_wl_packets_field_for_field() -> None:
    """Every recorded `WL-` request in `captures/` must match `build_request` field-for-field."""
    matched = 0
    seen: set[str] = set()
    for path in sorted(CAPTURES.glob("*.jsonl")):
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            rec = json.loads(raw_line)
            if "/rpc/request/WL-" not in rec.get("topic", ""):
                continue
            expected_packet = json.loads(rec["payload"])
            body = expected_packet["Body"]
            op_id = body["requestId"]
            if op_id in seen:
                continue
            seen.add(op_id)

            action = expected_packet["Type"]
            params = body.get(action)
            topic, built_packet = build_request(
                expected_packet["DeviceCode"],
                action,
                params,
                op_id,
                body["timeout"],
                msg_id=expected_packet["MsgId"],
                timestamp_ms=expected_packet["Timestamp"],
            )
            assert topic == rec["topic"]
            assert built_packet == expected_packet
            assert list(built_packet.keys()) == list(expected_packet.keys())
            assert list(built_packet["Body"].keys()) == list(body.keys())
            matched += 1

    assert matched >= 28


def test_pcr_rpc_results_success_and_failure_from_captures() -> None:
    # Success cases: Lift, OpenDoor, CloseDoor(WaitLid=false), ListFiles
    for fname, line in [
        ("pcr_door_cycle_175845.jsonl", 7),
        ("pcr_door_cycle_175845.jsonl", 16),
        ("pcr_door_cycle_175845.jsonl", 25),
        ("pcr_readonly_programs.jsonl", 6),
    ]:
        rec = _capture_line(fname, line)
        parsed = parse_uplink(rec["topic"], rec["payload"])
        assert isinstance(parsed, RpcResult)
        assert parsed.ok is True
        assert parsed.error == ""
        assert parsed.result_topic == "device"

    # Failure cases where outer Data.success is True, but deviceData.success is False!
    for fname, line, snippet in [
        ("lab_v1_20261117_173423.jsonl", 610, "未知功能: GetProperty"),
        ("pcr_door_cycle_175738.jsonl", 8, "等待应答超时"),
        ("pcr_door_test.jsonl", 55, "热盖抬起"),
    ]:
        rec = _capture_line(fname, line)
        parsed = parse_uplink(rec["topic"], rec["payload"])
        assert isinstance(parsed, RpcResult)
        assert parsed.ok is False
        assert snippet in parsed.error


def test_shaker_action_and_get_status_from_captures() -> None:
    # Read queries: GetStatus (line 30) and GetErrors (line 36) have no `ok` field
    status_rec = _capture_line("test_session.jsonl", 30)
    status_res = parse_uplink(status_rec["topic"], status_rec["payload"])
    assert isinstance(status_res, RpcResult)
    assert status_res.ok is True
    assert status_res.data["BlockTemperature"] == 30.0
    assert status_res.data["ElmState"] == 2

    err_rec = _capture_line("test_session.jsonl", 36)
    err_res = parse_uplink(err_rec["topic"], err_rec["payload"])
    assert isinstance(err_res, RpcResult)
    assert err_res.ok is True
    assert err_res.data["errorCode"] == 0

    # Action RPCs: SetClamp (96), SetTargetSpeed (105), StartShakeTimed (120) check `deviceData.ok`
    for line in (96, 105, 120):
        rec = _capture_line("test_session.jsonl", line)
        parsed = parse_uplink(rec["topic"], rec["payload"])
        assert isinstance(parsed, RpcResult)
        assert parsed.ok is True
        assert parsed.data["ok"] is True

    ok, reason = shaker.spec.result_ok(
        "SetClamp",
        {"success": True, "deviceData": {"ok": False, "error": "夹紧超时"}},
    )
    assert ok is False
    assert "夹紧超时" in reason


def test_string_device_data_and_missing_data_field() -> None:
    # Real captured string deviceData ("ERROR: device not found") with outer Data.success=True
    rec = _capture_line("lab_v1_20261117_173423.jsonl", 842)
    parsed = parse_uplink(rec["topic"], rec["payload"])
    assert isinstance(parsed, RpcResult)
    assert parsed.ok is False
    assert parsed.error == "ERROR: device not found"

    # Gateway exception branch without `Data`: judged by `Body.code == "0"`
    ok_pkt = {
        "MsgId": "m1",
        "Timestamp": 1794908056421,
        "GatewayCode": "GW_001",
        "DeviceCode": "PCR002",
        "Type": "rpc-result",
        "Body": {"ResponseId": "WL-1-1", "Method": "GetDoor", "code": "0", "msg": "ok"},
    }
    res_ok = parse_uplink("lab/v1/devices/PCR002/rpc/response/WL-1-1", json.dumps(ok_pkt))
    assert isinstance(res_ok, RpcResult)
    assert res_ok.ok is True

    fail_pkt = {
        **ok_pkt,
        "Body": {"ResponseId": "WL-1-2", "Method": "GetDoor", "code": "500", "msg": "gateway busy"},
    }
    res_fail = parse_uplink("lab/v1/devices/PCR002/rpc/response/WL-1-2", json.dumps(fail_pkt))
    assert isinstance(res_fail, RpcResult)
    assert res_fail.ok is False
    assert res_fail.error == "gateway busy"


def test_arm_mov_multi_lines_58_59_and_1458_1459_produce_single_motion_result() -> None:
    # Lines 58 (device topic) + 59 (motion/response): success
    l58 = _capture_line("lab_v1_20261117_173423.jsonl", 58)
    l59 = _capture_line("lab_v1_20261117_173423.jsonl", 59)
    results_ok = [
        r
        for rec in (l58, l59)
        if isinstance((r := parse_uplink(rec["topic"], rec["payload"])), RpcResult)
    ]
    assert len(results_ok) == 1
    assert results_ok[0].result_topic == "motion/response"
    assert results_ok[0].request_id == "S-2122624185093070850"
    assert results_ok[0].ok is True

    # Lines 1458 (device topic) + 1459 (motion/response): failure ("前往 失败")
    l1458 = _capture_line("lab_v1_20261117_173423.jsonl", 1458)
    l1459 = _capture_line("lab_v1_20261117_173423.jsonl", 1459)
    results_fail = [
        r
        for rec in (l1458, l1459)
        if isinstance((r := parse_uplink(rec["topic"], rec["payload"])), RpcResult)
    ]
    assert len(results_fail) == 1
    assert results_fail[0].result_topic == "motion/response"
    assert results_fail[0].request_id == "S-2122625672602984449"
    assert results_fail[0].ok is False
    assert results_fail[0].error == "前往 失败"


def test_gateway_builtins_rpc_acks_and_other_gateways_in_captures() -> None:
    # Gateway built-ins GetDevices (730) & GetDrivers (756) return list deviceData
    for line in (730, 756):
        rec = _capture_line("lab_v1_20261117_173423.jsonl", line)
        parsed = parse_uplink(rec["topic"], rec["payload"])
        assert isinstance(parsed, RpcResult)
        assert parsed.ok is True
        assert isinstance(parsed.data["deviceData"], list)

    # All 3 gateway rpc-response acks (607 GW_002, 608 GW_005, 609 GW_001) are ignored
    for line in (607, 608, 609):
        rec = _capture_line("lab_v1_20261117_173423.jsonl", line)
        assert isinstance(parse_uplink(rec["topic"], rec["payload"]), IgnoredFrame)

    # GW_005 WASHER001 rpc-result (line 1059) is discarded
    washer = _capture_line("lab_v1_20261117_173423.jsonl", 1059)
    assert parse_uplink(washer["topic"], washer["payload"]) == IgnoredFrame("other_gateway")

    # Arm device-topic failure where Data.success=False and deviceData=None (line 1811)
    arm_fail = _capture_line("lab_v1_20261117_173423.jsonl", 1811)
    arm_res = parse_uplink(arm_fail["topic"], arm_fail["payload"])
    assert isinstance(arm_res, RpcResult)
    assert arm_res.ok is False
    assert "20018" in arm_res.error


BASELINE_CAPTURES = (
    "lab_v1_20261117_173423.jsonl",
    "pcr_close_again.jsonl",
    "pcr_diag_175831.jsonl",
    "pcr_door_cycle_175738.jsonl",
    "pcr_door_cycle_175845.jsonl",
    "pcr_door_test.jsonl",
    "pcr_lid_watch.jsonl",
    "pcr_open_again.jsonl",
    "pcr_readonly_programs.jsonl",
    "pcr_watch2.jsonl",
    "points_wide_184152.jsonl",
    "test_session.jsonl",
)


def test_device_spec_result_ok_contracts_and_zero_unrecognized_across_all_captures() -> None:
    pcr_err = {"success": True, "deviceData": {"success": False, "error": "x"}}
    assert pcr.spec.result_ok("OpenDoor", pcr_err) == (False, "x")
    assert arm.spec.result_ok("MovMulti", {"code": "0", "msg": "ok"}) == (True, "")
    assert arm.spec.result_ok("MovMulti", {"code": "1", "msg": "err"}) == (False, "err")
    assert gateway.spec.result_ok("GetDevices", {"success": True, "deviceData": []}) == (True, "")
    tt_ok = {"success": True, "deviceData": {"success": True}}
    assert turntable.spec.result_ok("SingleLoop", tt_ok) == (True, "")
    rack_ok = {"success": True, "deviceData": {"rackState": 0}}
    assert rack.spec.result_ok("GetStatus", rack_ok) == (True, "")

    unrecognized = []
    total = 0
    for fname in BASELINE_CAPTURES:
        path = CAPTURES / fname
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            total += 1
            rec = json.loads(line)
            frame = parse_uplink(rec["topic"], rec["payload"])
            if isinstance(frame, UnrecognizedFrame):
                unrecognized.append((path.name, i, frame.reason))

    assert total > 2000
    assert unrecognized == []
