"""Pure MQTT packet assembly and two-layer uplink parsing (docs/spec/04 section 5.12)."""

from dataclasses import dataclass
import json
import time
from typing import Any
import uuid

from ..devices import BY_CODE, gateway

TOPIC_ROOT = "lab/v1"
GATEWAY_CODE = "GW_001"
GATEWAY_TELEMETRY_TOPIC = f"{TOPIC_ROOT}/gateway/{GATEWAY_CODE}/telemetry"


@dataclass(frozen=True)
class GatewayHeartbeat:
    """Heartbeat frame received on `lab/v1/gateway/GW_001/telemetry`."""


@dataclass(frozen=True)
class DeviceTelemetry:
    """Incremental property frame received on `lab/v1/devices/<device>/telemetry`."""

    device_code: str
    props: dict[str, Any]


@dataclass(frozen=True)
class ExternalRequest:
    """Non-`WL-` command observed on a `GW_001` device or `motion/request` topic."""

    request_id: str
    device_code: str | None
    action: str | None
    result_topic: str


@dataclass(frozen=True)
class RpcResult:
    """Final RPC outcome after both gateway-layer and device `spec.result_ok` checks."""

    request_id: str
    device_code: str | None
    action: str | None
    result_topic: str
    ok: bool
    data: dict[str, Any]
    error: str


@dataclass(frozen=True)
class IgnoredFrame:
    """Known valid frame intentionally ignored (e.g. non-GW_001, `rpc-response`, progress)."""

    reason: str


@dataclass(frozen=True)
class UnrecognizedFrame:
    """Malformed or unrecognized frame to be archived and counted without crashing."""

    topic: str
    raw: str
    reason: str


ParsedFrame = (
    GatewayHeartbeat
    | DeviceTelemetry
    | ExternalRequest
    | RpcResult
    | IgnoredFrame
    | UnrecognizedFrame
)


def build_request(
    device_code: str,
    action: str,
    params: dict[str, Any] | None,
    operation_id: str,
    timeout_s: float,
    *,
    msg_id: str | None = None,
    timestamp_ms: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Assemble `(topic, packet)` for one outgoing device RPC command (01 section 3.3)."""
    mid = msg_id if msg_id is not None else uuid.uuid4().hex
    ts_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    topic = f"{TOPIC_ROOT}/devices/{device_code}/rpc/request/{operation_id}"
    body: dict[str, Any] = {
        "msgId": mid,
        "requestId": operation_id,
        "ts": ts_ms,
        "deviceCode": device_code,
        "action": action,
        "timeout": int(timeout_s),
    }
    if params:
        body[action] = dict(params)
    packet: dict[str, Any] = {
        "MsgId": mid,
        "Timestamp": ts_ms,
        "GatewayCode": GATEWAY_CODE,
        "DeviceCode": device_code,
        "Type": action,
        "Body": body,
    }
    return topic, packet


def _parse_rpc_result(
    topic: str,
    raw_text: str,
    msg: dict[str, Any],
    body: dict[str, Any],
    result_topic: str,
    topic_req_id: str,
    topic_dev: str | None,
) -> ParsedFrame:
    req_id = body.get("ResponseId") or body.get("requestId") or topic_req_id
    if not isinstance(req_id, str) or not req_id:
        return UnrecognizedFrame(topic, raw_text, "rpc-result missing requestId")

    dev_raw = body.get("DeviceId") or msg.get("DeviceCode") or topic_dev
    device_code = str(dev_raw) if dev_raw is not None else None
    act_raw = body.get("Method") or body.get("action")
    action = str(act_raw) if act_raw is not None else None

    if result_topic == "device" and device_code in BY_CODE and action is not None:
        act_spec = BY_CODE[device_code].spec.ACTIONS.get(action)
        if act_spec is not None and act_spec.result_topic != "device":
            return IgnoredFrame("awaiting_motion_response")

    if "Data" not in body or body["Data"] is None:
        if "code" not in body:
            return UnrecognizedFrame(topic, raw_text, "rpc-result has neither Data nor code")
        code_str = str(body["code"])
        ok = code_str == "0"
        error = "" if ok else str(body.get("msg") or body.get("description") or f"code={code_str}")
        inner = body.get("data")
        result_data = (
            dict(inner) if isinstance(inner, dict) else {"code": code_str, "msg": body.get("msg", "")}
        )
        return RpcResult(req_id, device_code, action, result_topic, ok, result_data, error)

    data = body["Data"]
    if not isinstance(data, dict):
        return UnrecognizedFrame(topic, raw_text, "rpc-result Body.Data is not dict")

    dd = data.get("deviceData")
    if isinstance(dd, str):
        return RpcResult(req_id, device_code, action, result_topic, False, {"deviceData": dd}, dd)

    if action in gateway.spec.ACTIONS:
        ok, error = gateway.spec.result_ok(action, data)
    elif device_code in BY_CODE:
        ok, error = BY_CODE[device_code].spec.result_ok(action or "", data)
    else:
        ok = data.get("success") is True
        error = "" if ok else str(data.get("description") or f"unknown device {device_code}")

    if isinstance(dd, dict):
        result_data = dict(dd)
    elif dd is not None:
        result_data = {"deviceData": dd}
    else:
        result_data = dict(data)
    return RpcResult(req_id, device_code, action, result_topic, ok, result_data, error)


def _parse_rpc_channel(
    topic: str,
    raw_text: str,
    msg: dict[str, Any],
    kind: str,
    topic_req_id: str,
    result_topic: str,
    topic_dev: str | None,
) -> ParsedFrame:
    msg_type = msg.get("Type")
    body = msg.get("Body")
    if not isinstance(body, dict):
        return UnrecognizedFrame(topic, raw_text, "RPC Body is not dict")
    if kind == "request":
        if not isinstance(msg_type, str) or not msg_type:
            return UnrecognizedFrame(topic, raw_text, "missing request Type")
        req_id = str(body.get("requestId") or topic_req_id)
        if req_id.startswith("WL-"):
            return IgnoredFrame("own_request")
        action = str(body.get("action") or msg_type)
        dev_code = topic_dev or (str(msg["DeviceCode"]) if msg.get("DeviceCode") else None)
        expected_topic = result_topic
        if expected_topic == "device" and dev_code in BY_CODE:
            act_spec = BY_CODE[dev_code].spec.ACTIONS.get(action)
            if act_spec is not None:
                expected_topic = act_spec.result_topic
        return ExternalRequest(req_id, dev_code, action, expected_topic)
    if kind == "response":
        if msg_type == "rpc-response":
            return IgnoredFrame("rpc_ack")
        if msg_type == "rpc-result":
            return _parse_rpc_result(topic, raw_text, msg, body, result_topic, topic_req_id, topic_dev)
        return UnrecognizedFrame(topic, raw_text, f"unexpected response Type={msg_type!r}")
    if kind == "progress" and msg_type == "rpc-progress":
        return IgnoredFrame("progress")
    return UnrecognizedFrame(topic, raw_text, "unrecognized RPC subtopic")


def parse_uplink(topic: str, payload: bytes | str) -> ParsedFrame:
    """Two-layer uplink parser: gateway envelope filter/rules then `spec.result_ok`."""
    if isinstance(payload, bytes):
        try:
            raw_text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return UnrecognizedFrame(topic, payload.hex(), "invalid utf-8")
    else:
        raw_text = payload

    try:
        msg = json.loads(raw_text)
    except json.JSONDecodeError:
        return UnrecognizedFrame(topic, raw_text, "invalid json")
    if not isinstance(msg, dict):
        return UnrecognizedFrame(topic, raw_text, "payload is not a JSON object")

    gw = msg.get("GatewayCode")
    if gw is None and topic.startswith(f"{TOPIC_ROOT}/gateway/") and "/driver/" in topic:
        gw_lower = msg.get("gatewayCode")
        if isinstance(gw_lower, str) and gw_lower:
            if gw_lower != GATEWAY_CODE:
                return IgnoredFrame("other_gateway")
            if msg.get("type") in ("driver_capability", "driver_list"):
                return IgnoredFrame("driver_metadata")

    if not isinstance(gw, str) or not gw:
        return UnrecognizedFrame(topic, raw_text, "missing GatewayCode")
    if gw != GATEWAY_CODE:
        return IgnoredFrame("other_gateway")

    parts = topic.split("/")
    if len(parts) < 5 or parts[0] != "lab" or parts[1] != "v1":
        return UnrecognizedFrame(topic, raw_text, "unexpected topic root")

    msg_type = msg.get("Type")
    body = msg.get("Body")

    if parts[2] == "gateway":
        if parts[3] != GATEWAY_CODE:
            return UnrecognizedFrame(topic, raw_text, "gateway topic mismatch")
        if len(parts) == 5 and parts[4] == "telemetry":
            if msg_type == "heartbeat" and isinstance(body, dict):
                return GatewayHeartbeat()
            return UnrecognizedFrame(topic, raw_text, "invalid gateway telemetry")
        if len(parts) == 7 and parts[4] == "motion" and parts[6]:
            return _parse_rpc_channel(
                topic, raw_text, msg, parts[5], parts[6], "motion/response", None
            )
        if len(parts) >= 6 and parts[4] == "device" and isinstance(msg_type, str):
            if isinstance(body, dict):
                return IgnoredFrame("gateway_device_mgmt")
        if len(parts) == 5 and parts[4] == "location" and isinstance(msg_type, str):
            return IgnoredFrame("gateway_location")
        return UnrecognizedFrame(topic, raw_text, "unrecognized gateway topic")

    if parts[2] == "devices" and parts[3]:
        dev = parts[3]
        if len(parts) == 5 and parts[4] == "telemetry":
            if msg_type != "telemetry" or not isinstance(body, dict):
                return UnrecognizedFrame(topic, raw_text, "invalid device telemetry")
            items = body.get("Body")
            if not isinstance(items, list):
                return UnrecognizedFrame(topic, raw_text, "telemetry Body.Body is not list")
            props: dict[str, Any] = {}
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("Code"), str) or "Value" not in item:
                    return UnrecognizedFrame(topic, raw_text, "malformed telemetry item")
                props[item["Code"]] = item["Value"]
            dev_code = str(body.get("DeviceCode") or msg.get("DeviceCode") or dev)
            return DeviceTelemetry(dev_code, props)
        if len(parts) == 5 and parts[4] == "status":
            if msg_type in ("online", "offline") and isinstance(body, dict):
                return IgnoredFrame("device_status")
            return UnrecognizedFrame(topic, raw_text, "invalid device status")
        if len(parts) == 7 and parts[4] == "rpc" and parts[6]:
            return _parse_rpc_channel(topic, raw_text, msg, parts[5], parts[6], "device", dev)

    return UnrecognizedFrame(topic, raw_text, "unrecognized topic")
