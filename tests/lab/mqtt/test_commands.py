"""Tests for `CommandDispatcher` with caller-supplied `operation_id` (#12)."""

import json
from pathlib import Path
import threading
from typing import Any

import pytest

from wetlab.lab.errors import DeviceFailed, NoResult
from wetlab.lab.mqtt.codec import RpcResult, parse_uplink
from wetlab.lab.mqtt.commands import CommandDispatcher

CAPTURES = Path(__file__).resolve().parents[3] / "captures"


def _capture_line(filename: str, lineno: int) -> dict:
    lines = (CAPTURES / filename).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[lineno - 1])


def _rewrite_op_id(rec: dict, new_op_id: str) -> tuple[str, str]:
    parts = rec["topic"].split("/")
    parts[-1] = new_op_id
    payload = json.loads(rec["payload"])
    body = payload["Body"]
    if "ResponseId" in body:
        body["ResponseId"] = new_op_id
    if "requestId" in body:
        body["requestId"] = new_op_id
    return "/".join(parts), json.dumps(payload, ensure_ascii=False)


def test_no_result_raises_timeout_publishes_once_and_uses_exact_operation_id() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    dispatcher: CommandDispatcher

    def publish(topic: str, packet: dict[str, Any]) -> None:
        published.append((topic, packet))
        op_id = packet["Body"]["requestId"]
        for line in (607, 608, 609):
            t, p = _rewrite_op_id(_capture_line("lab_v1_20261117_173423.jsonl", line), op_id)
            frame = parse_uplink(t, p)
            if isinstance(frame, RpcResult):
                dispatcher.on_result(frame)

    dispatcher = CommandDispatcher(publish, now_fn=lambda: 1794909000.0)
    assert not hasattr(dispatcher, "_next_operation_id")
    assert not hasattr(dispatcher, "_seq")

    with pytest.raises(NoResult):
        dispatcher.call(
            "PCR002",
            "OpenDoor",
            {"TimeoutMs": 60000},
            0.04,
            operation_id="WL-1794909000000-1",
        )

    assert len(published) == 1
    assert published[0][1]["Body"]["requestId"] == "WL-1794909000000-1"


def test_device_failure_raises_device_failed_without_retrying() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    dispatcher: CommandDispatcher

    def publish(topic: str, packet: dict[str, Any]) -> None:
        published.append((topic, packet))
        op_id = packet["Body"]["requestId"]
        t, p = _rewrite_op_id(_capture_line("pcr_door_cycle_175738.jsonl", 8), op_id)
        frame = parse_uplink(t, p)
        assert isinstance(frame, RpcResult)
        dispatcher.on_result(frame)

    dispatcher = CommandDispatcher(publish, now_fn=lambda: 1794909458.723)
    with pytest.raises(DeviceFailed, match="等待应答超时"):
        dispatcher.call(
            "PCR002",
            "SetLidMotor",
            {"Action": "Lift"},
            1.0,
            operation_id="WL-1794909458723-2",
        )

    assert len(published) == 1
    assert published[0][1]["Body"]["requestId"] == "WL-1794909458723-2"


def test_mov_multi_ignores_device_topic_result_and_resolves_on_motion_response() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    step_events = [threading.Event(), threading.Event()]
    dispatcher: CommandDispatcher

    def publish_success(topic: str, packet: dict[str, Any]) -> None:
        published.append((topic, packet))
        op_id = packet["Body"]["requestId"]

        def feed() -> None:
            t58, p58 = _rewrite_op_id(_capture_line("lab_v1_20261117_173423.jsonl", 58), op_id)
            assert not isinstance(parse_uplink(t58, p58), RpcResult)
            dispatcher.on_result(
                RpcResult(op_id, "TYPE_ROBOT_ARM001", "MovMulti", "device", True, {"wrong": 1}, "")
            )
            step_events[0].set()
            step_events[1].wait(timeout=1.0)
            t59, p59 = _rewrite_op_id(_capture_line("lab_v1_20261117_173423.jsonl", 59), op_id)
            frame59 = parse_uplink(t59, p59)
            assert isinstance(frame59, RpcResult)
            dispatcher.on_result(frame59)

        threading.Thread(target=feed, daemon=True).start()

    dispatcher = CommandDispatcher(publish_success, now_fn=lambda: 1794908056.0)
    outcome: dict[str, Any] = {}
    done = threading.Event()

    def invoke() -> None:
        outcome["res"] = dispatcher.call(
            "TYPE_ROBOT_ARM001",
            "MovMulti",
            {"Tasks": []},
            1.0,
            operation_id="WL-1794908056000-3",
        )
        done.set()

    worker = threading.Thread(target=invoke)
    worker.start()
    assert step_events[0].wait(timeout=1.0)
    assert not done.wait(timeout=0.03)
    step_events[1].set()
    worker.join(timeout=1.0)
    assert done.is_set()
    assert outcome["res"]["code"] == "0"
    assert len(published) == 1
    assert published[0][1]["Body"]["requestId"] == "WL-1794908056000-3"

    # Replay lines 1458 + 1459 failure ("前往 失败")
    published.clear()

    def publish_fail(topic: str, packet: dict[str, Any]) -> None:
        published.append((topic, packet))
        op_id = packet["Body"]["requestId"]
        for line in (1458, 1459):
            t, p = _rewrite_op_id(_capture_line("lab_v1_20261117_173423.jsonl", line), op_id)
            frame = parse_uplink(t, p)
            if isinstance(frame, RpcResult):
                dispatcher.on_result(frame)

    dispatcher = CommandDispatcher(publish_fail, now_fn=lambda: 1794908498.0)
    with pytest.raises(DeviceFailed, match="前往 失败"):
        dispatcher.call(
            "TYPE_ROBOT_ARM001",
            "MovMulti",
            {"Tasks": []},
            1.0,
            operation_id="WL-1794908498000-4",
        )
    assert len(published) == 1
